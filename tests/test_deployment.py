import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("deployment", Path(__file__).resolve().parents[1] / "deploy/deploy.py")
deployment = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deployment)


def payload():
    return {
        "action": "deploy",
        "image": "ghcr.io/uburuntu/msu_hub_bot@sha256:" + "a" * 64,
        "revision": "b" * 40,
        "environment": {"HUB_BOT_TOKEN": "fake", "HUB_REDIS_HOST": "localhost", "HUB_EDGEDB_DSN": "fake"},
        "registry_username": "example",
        "registry_token": "fake",
    }


@pytest.mark.parametrize("image", ["ghcr.io/elsewhere/bot:latest", "ghcr.io/uburuntu/msu_hub_bot:latest", "$(touch /tmp/pwned)"])
def test_rejects_mutable_or_foreign_images(image):
    request = payload()
    request["image"] = image
    with pytest.raises(deployment.DeploymentError):
        deployment.validate_payload(request)


def test_compose_only_manages_the_bot_and_literal_configuration(tmp_path):
    document = deployment.compose_document(payload()["image"], tmp_path / "runtime.env")
    assert set(document["services"]) == {"bot"}
    assert document["networks"] == {"msu_db": {"external": True}}
    assert document["services"]["bot"]["env_file"][0]["format"] == "raw"
    assert "volumes" not in document
    assert "ports" not in document["services"]["bot"]


def test_failed_cutover_restores_legacy_after_stopping_replacement(tmp_path):
    class Fake(deployment.Deployer):
        def __init__(self):
            super().__init__(tmp_path)
            self.events = []

        def run(self, *args, **kwargs):
            return ""

        def inspect(self, name):
            return {"State": {"Running": True}, "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}}}

        def compose(self, state, *args, **kwargs):
            self.events.append(args[0])

        def stop_legacy(self):
            self.events.append("stop_legacy")

        def stop_replacement(self):
            self.events.append("stop_replacement")

        def wait_healthy(self, **kwargs):
            raise deployment.DeploymentError("not ready")

        def restore(self, previous):
            assert previous["legacy"]
            self.stop_replacement()
            self.events.append("start_legacy")

    fake = Fake()
    with pytest.raises(deployment.DeploymentError, match="rolled back"):
        fake.deploy(payload())
    assert fake.events == ["config", "run", "stop_legacy", "stop_replacement", "up", "stop_replacement", "start_legacy"]
    assert not (tmp_path / "current.json").exists()
    runtime = next((tmp_path / "releases").glob("*/runtime.env"))
    assert runtime.stat().st_mode & 0o777 == 0o600
    assert json.loads(runtime.read_text().split("=", 1)[1])["HUB_BOT_TOKEN"] == "fake"


def test_rollback_from_legacy_stops_it_before_starting_an_extracted_release(tmp_path):
    events = []
    deployer = deployment.Deployer(tmp_path)
    deployer.stop_replacement = lambda: events.append("stop_replacement")
    deployer.stop_legacy = lambda: events.append("stop_legacy")
    deployer.compose = lambda *args: events.append("start_extracted")
    deployer.wait_healthy = lambda: events.append("ready")
    deployer.restore({"release": "prior"})
    assert events == ["stop_replacement", "stop_legacy", "start_extracted", "ready"]


def test_first_deployment_on_a_fresh_host_and_unavailable_rollback(tmp_path):
    deployer = deployment.Deployer(tmp_path)
    deployer.run = lambda *args, **kwargs: ""
    deployer.inspect = lambda name: None
    deployer.compose = lambda *args, **kwargs: ""
    deployer.wait_healthy = lambda: None
    deployer.deploy(payload())
    assert deployer.read_state("current.json")["image"] == payload()["image"]
    assert deployer.read_state("previous.json") == {"empty": True}
    with pytest.raises(deployment.DeploymentError, match="No prior release"):
        deployer.deploy({"action": "rollback"})


def test_failed_manual_rollback_restores_current_release(tmp_path):
    previous, current = {"release": "old"}, {"release": "current"}
    deployment.write_private(tmp_path / "previous.json", json.dumps(previous))
    deployment.write_private(tmp_path / "current.json", json.dumps(current))
    deployer = deployment.Deployer(tmp_path)
    events = []

    def restore(state):
        events.append(state["release"])
        if state == previous:
            raise deployment.DeploymentError("not ready")

    deployer.restore = restore
    with pytest.raises(deployment.DeploymentError, match="current release restored"):
        deployer.deploy({"action": "rollback"})
    assert events == ["old", "current"]
    assert deployer.read_state("current.json") == current
