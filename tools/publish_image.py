"""Build, check, and publish the same image; expose only its immutable digest."""

import json
import os
import subprocess
import tempfile
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def package_metadata(owner, name):
    result = subprocess.run(["gh", "api", f"users/{owner}/packages/container/{name}"], capture_output=True, text=True)
    data = json.loads(result.stdout) if result.stdout else {}
    if result.returncode:
        if data.get("status") in {404, "404"}:
            return None
        raise SystemExit("Cannot verify package visibility")
    return data


def require_anonymous_denial(repository, digest):
    scope = "repository:" + repository.removeprefix("ghcr.io/") + ":pull"
    try:
        with urlopen("https://ghcr.io/token?" + urlencode({"service": "ghcr.io", "scope": scope}), timeout=20) as response:
            token = json.load(response)["token"]
        from urllib.request import Request

        request = Request(
            "https://ghcr.io/v2/" + repository.removeprefix("ghcr.io/") + "/manifests/" + digest,
            headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.docker.distribution.manifest.v2+json"},
        )
        with urlopen(request, timeout=20):
            raise SystemExit("Registry permits anonymous image access")
    except HTTPError as error:
        if error.code not in {401, 403, 404}:
            raise SystemExit("Anonymous registry check was inconclusive") from None


def image_digest(image, repository):
    metadata = json.loads(run("docker", "image", "inspect", image, capture_output=True, text=True).stdout)[0]
    return next(ref for ref in metadata["RepoDigests"] if ref.startswith(repository + "@sha256:"))


def main():
    owner, name = os.environ["GITHUB_REPOSITORY"].split("/")
    repository = "ghcr.io/" + os.environ["GITHUB_REPOSITORY"].lower()
    image = repository + ":" + os.environ["GITHUB_SHA"]
    package = package_metadata(owner, name)
    if package is None:
        # An empty image establishes access controls before any application or SDK is uploaded.
        with tempfile.TemporaryDirectory(prefix="msu-hub-package-") as directory:
            directory = Path(directory)
            (directory / "Dockerfile").write_text('FROM scratch\nLABEL description="Private package bootstrap"\n')
            bootstrap = repository + ":bootstrap"
            run("docker", "build", "--platform", "linux/amd64", "--tag", bootstrap, str(directory))
            push(bootstrap)
        package = package_metadata(owner, name)
        require_anonymous_denial(repository, image_digest(bootstrap, repository).split("@")[1])
    if not package or package["visibility"] != "private":
        raise SystemExit("Image publication requires a private package")
    run(
        "docker",
        "build",
        "--platform",
        "linux/amd64",
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
    push(image)
    package = package_metadata(owner, name)
    digest = image_digest(image, repository)
    if package["visibility"] != "private":
        raise SystemExit("Package visibility changed during publication")
    require_anonymous_denial(repository, digest.split("@")[1])
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        output.write("image=" + digest + "\n")
    print("Validated private image published: " + digest)


def push(image):
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


if __name__ == "__main__":
    main()
