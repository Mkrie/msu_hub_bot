#!/usr/bin/env python3
"""Restricted SSH entrypoint. Its only authority is this bot's release operation.

Install under ~/msu_hub_bot, with this exact file as the SSH forced command.
It never evaluates SSH_ORIGINAL_COMMAND or accepts shell commands or host paths.
"""

import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

IMAGE_RE = re.compile(r"ghcr\.io/uburuntu/msu_hub_bot@sha256:[0-9a-f]{64}")
REVISION_RE = re.compile(r"[0-9a-f]{40}")
CONTAINER = "msu_hub_bot"
LEGACY = "hub_bot"


class DeploymentError(Exception):
    pass


def validate_payload(payload):
    if not isinstance(payload, dict) or payload.get("action") not in {"deploy", "rollback"}:
        raise DeploymentError("Unknown operation")
    if payload["action"] == "rollback":
        if set(payload) != {"action"}:
            raise DeploymentError("Unexpected rollback fields")
        return
    if set(payload) != {"action", "image", "revision", "environment", "registry_username", "registry_token"}:
        raise DeploymentError("Unexpected deployment fields")
    if not isinstance(payload["image"], str) or not IMAGE_RE.fullmatch(payload["image"]):
        raise DeploymentError("Image must be an immutable digest in this bot's repository")
    if not isinstance(payload["revision"], str) or not REVISION_RE.fullmatch(payload["revision"]):
        raise DeploymentError("Invalid source revision")
    values = payload["environment"]
    if not isinstance(values, dict) or not values:
        raise DeploymentError("Missing runtime configuration")
    for key, value in values.items():
        if not re.fullmatch(r"HUB_[A-Z0-9_]+", key) or key == "HUB_CONFIG_JSON" or not isinstance(value, str) or "\0" in value:
            raise DeploymentError("Invalid runtime configuration")
    if not all(values.get(key) for key in ("HUB_BOT_TOKEN", "HUB_REDIS_HOST", "HUB_EDGEDB_DSN")):
        raise DeploymentError("Missing core runtime settings")
    if not re.fullmatch(r"[A-Za-z0-9-]+", payload["registry_username"]) or not isinstance(payload["registry_token"], str):
        raise DeploymentError("Invalid registry authentication")


def write_private(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as f:
        f.write(data)
        os.fchmod(f.fileno(), 0o600)
        staging = Path(f.name)
    staging.replace(path)


def compose_document(image, env_path):
    return {
        "services": {
            "bot": {
                "image": image,
                "container_name": CONTAINER,
                "env_file": [{"path": str(env_path), "format": "raw"}],
                "restart": "unless-stopped",
                "networks": ["msu_db"],
                "read_only": True,
                "tmpfs": ["/tmp:mode=1777", "/work:mode=1777"],
                "security_opt": ["no-new-privileges:true"],
                "cap_drop": ["ALL"],
                "ulimits": {"core": 0},
                "stop_grace_period": "90s",
                "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}},
            }
        },
        "networks": {"msu_db": {"external": True}},
    }


class Deployer:
    def __init__(self, root):
        self.root = Path(root).resolve()

    def run(self, *args, input=None, timeout=180, check=True):
        try:
            result = subprocess.run(args, input=input, text=True, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise DeploymentError("Operation timed out") from None
        if check and result.returncode:
            # Docker/Compose errors can quote configuration: keep raw output private.
            raise DeploymentError("Docker operation failed")
        return result.stdout if result.returncode == 0 else None

    def inspect(self, name):
        data = self.run("docker", "inspect", name, check=False)
        return json.loads(data)[0] if data else None

    def read_state(self, filename):
        path = self.root / filename
        return json.loads(path.read_text()) if path.exists() else None

    def compose(self, state, *args, timeout=180):
        directory = (self.root / "releases" / state["release"]).resolve()
        if directory.parent != self.root / "releases":
            raise DeploymentError("Invalid stored release path")
        return self.run("docker", "compose", "--project-name", CONTAINER, "--file", str(directory / "compose.json"), *args, timeout=timeout)

    def stop_replacement(self):
        if self.inspect(CONTAINER):
            self.run("docker", "stop", "--time", "90", CONTAINER, timeout=110)
            self.run("docker", "rm", CONTAINER)

    def stop_legacy(self):
        info = self.inspect(LEGACY)
        if not info or not info["State"]["Running"]:
            return
        self.run("docker", "update", "--restart=no", LEGACY)
        # The legacy shell entrypoint does not forward signals to Python.
        signal_python = """import os,signal
from pathlib import Path
for path in Path('/proc').iterdir():
    if not path.name.isdigit() or int(path.name)==os.getpid():
        continue
    try:
        args=(path/'cmdline').read_bytes().split(b'\\0')
        if any(arg==b'main.py' or arg.endswith(b'/main.py') for arg in args):
            os.kill(int(path.name),signal.SIGTERM)
    except (OSError,ProcessLookupError):
        pass
"""
        self.run("docker", "exec", "-i", LEGACY, "python3", "-", input=signal_python, check=False)
        self.run("docker", "stop", "--time", "90", LEGACY, timeout=110)

    def wait_healthy(self, deadline=300):
        until = time.monotonic() + deadline
        while time.monotonic() < until:
            info = self.inspect(CONTAINER)
            if info:
                state = info["State"]
                if state.get("Health", {}).get("Status") == "healthy":
                    if info.get("RestartCount", 0):
                        raise DeploymentError("Replacement restarted during startup")
                    return
                if not state["Running"] or state.get("Health", {}).get("Status") == "unhealthy":
                    break
            time.sleep(3)
        raise DeploymentError("Replacement did not become ready")

    def restore(self, previous):
        self.stop_replacement()
        if previous.get("empty"):
            return
        if previous.get("legacy"):
            self.run("docker", "update", "--restart=" + previous["restart_policy"], LEGACY)
            self.run("docker", "start", LEGACY)
            if not self.inspect(LEGACY)["State"]["Running"]:
                raise DeploymentError("Legacy container did not restart")
        else:
            self.stop_legacy()
            self.compose(previous, "up", "--detach", "--no-deps", "bot")
            self.wait_healthy()

    def deploy(self, payload):
        validate_payload(payload)
        if payload["action"] == "rollback":
            previous, current = self.read_state("previous.json"), self.read_state("current.json")
            if not previous or previous.get("empty") or not current:
                raise DeploymentError("No prior release recorded")
            try:
                self.restore(previous)
            except Exception:
                print("Rollback failed; restoring the current release", flush=True)
                self.restore(current)
                raise DeploymentError("Rollback failed; current release restored") from None
            write_private(self.root / "current.json", json.dumps(previous))
            write_private(self.root / "previous.json", json.dumps(current))
            print("Rollback completed", flush=True)
            return
        self.run("docker", "network", "inspect", "msu_db")
        release = payload["revision"] + "-" + str(time.time_ns())
        directory = self.root / "releases" / release
        directory.mkdir(mode=0o700, parents=True)
        environment = dict(payload["environment"])
        environment["HUB_LOGS_FILE"] = "/tmp/msu_hub_bot.log"
        write_private(directory / "runtime.env", "HUB_CONFIG_JSON=" + json.dumps(environment, ensure_ascii=True) + "\n")
        write_private(directory / "compose.json", json.dumps(compose_document(payload["image"], directory / "runtime.env"), indent=2))
        state = {"release": release, "revision": payload["revision"], "image": payload["image"]}
        with tempfile.TemporaryDirectory(prefix="registry-", dir=self.root) as config:
            self.run(
                "docker",
                "--config",
                config,
                "login",
                "ghcr.io",
                "--username",
                payload["registry_username"],
                "--password-stdin",
                input=payload["registry_token"],
            )
            self.run("docker", "--config", config, "pull", payload["image"], timeout=600)
        print("Image pulled; checking configuration and connections", flush=True)
        self.compose(state, "config", "--quiet")
        self.compose(
            state,
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "--entrypoint",
            "/opt/msu_hub_bot/.venv/bin/python",
            "bot",
            "-m",
            "msu_hub_bot.preflight",
            timeout=180,
        )
        previous = self.read_state("current.json")
        if previous is None:
            legacy = self.inspect(LEGACY)
            previous = {"legacy": True, "restart_policy": legacy["HostConfig"]["RestartPolicy"]["Name"]} if legacy else {"empty": True}
        print("Preflight passed; stopping the current poller", flush=True)
        try:
            self.stop_legacy()
            self.stop_replacement()
            self.compose(state, "up", "--detach", "--no-deps", "bot")
            self.wait_healthy()
        except Exception:
            print("Release failed; restoring the previous poller", flush=True)
            self.restore(previous)
            print("Previous poller restored", flush=True)
            raise DeploymentError("Release failed and was rolled back") from None
        write_private(self.root / "previous.json", json.dumps(previous))
        write_private(self.root / "current.json", json.dumps(state))
        print("Deployed " + payload["revision"] + " " + payload["image"], flush=True)


def main():
    os.umask(0o077)
    root = Path(__file__).resolve().parent
    data = sys.stdin.read(131_073)
    if len(data) > 131_072:
        raise DeploymentError("Deployment request is too large")
    try:
        payload = json.loads(data)
    except ValueError:
        raise DeploymentError("Invalid deployment request") from None
    validate_payload(payload)
    with (root / "deployment.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        Deployer(root).deploy(payload)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(str(error) if isinstance(error, DeploymentError) else "Deployment failed; inspect the host privately", file=sys.stderr)
        raise SystemExit(1)
