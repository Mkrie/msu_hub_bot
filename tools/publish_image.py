"""Build, check, and publish the same image; expose only its immutable digest."""

import json
import os
import subprocess
import tempfile
from pathlib import Path


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def main():
    repository = "ghcr.io/" + os.environ["GITHUB_REPOSITORY"].lower()
    image = repository + ":" + os.environ["GITHUB_SHA"]
    run(
        "docker",
        "build",
        "--platform",
        "linux/amd64",
        "--label",
        "org.opencontainers.image.source=https://github.com/" + os.environ["GITHUB_REPOSITORY"],
        "--label",
        "org.opencontainers.image.revision=" + os.environ["GITHUB_SHA"],
        "--tag",
        image,
        ".",
    )
    with Path("tools/image_smoke.py").open() as source:
        run(
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp:mode=1777",
            "--tmpfs",
            "/work:mode=1777",
            "--entrypoint",
            "python",
            "-i",
            image,
            "-",
            stdin=source,
        )
    metadata = json.loads(run("docker", "image", "inspect", image, capture_output=True, text=True).stdout)[0]
    assert not any(value.startswith(("HUB_", "DEPLOY_", "GITHUB_TOKEN=", "GH_TOKEN=")) for value in metadata["Config"]["Env"])
    with tempfile.TemporaryDirectory(prefix="msu-hub-registry-") as config:
        run(
            "docker",
            "--config",
            config,
            "login",
            "ghcr.io",
            "--username",
            os.environ["GITHUB_ACTOR"],
            "--password-stdin",
            input=os.environ["GH_TOKEN"],
            text=True,
            capture_output=True,
        )
        run("docker", "--config", config, "push", image)
    metadata = json.loads(run("docker", "image", "inspect", image, capture_output=True, text=True).stdout)[0]
    digest = next(ref for ref in metadata["RepoDigests"] if ref.startswith(repository + "@sha256:"))
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        output.write("image=" + digest + "\n")
    print("Validated image published: " + digest)


if __name__ == "__main__":
    main()
