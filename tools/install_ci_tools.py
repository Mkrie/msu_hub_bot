"""Install checksum-pinned scanners into a caller-owned directory."""

import hashlib
import io
import subprocess
import sys
import tarfile
from pathlib import Path

TOOLS = (
    (
        "gitleaks",
        "https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_linux_x64.tar.gz",
        "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb",
    ),
    (
        "actionlint",
        "https://github.com/rhysd/actionlint/releases/download/v1.7.12/actionlint_1.7.12_linux_amd64.tar.gz",
        "8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8",
    ),
)


def main():
    directory = Path(sys.argv[1])
    directory.mkdir(parents=True, exist_ok=True)
    for name, url, digest in TOOLS:
        data = subprocess.run(
            ["curl", "--fail", "--silent", "--show-error", "--location", "--retry", "3", url], capture_output=True, check=True
        ).stdout
        if hashlib.sha256(data).hexdigest() != digest:
            raise SystemExit("Tool checksum verification failed")
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            member = next(m for m in archive.getmembers() if m.isfile() and m.name in {name, "./" + name})
            destination = directory / name
            destination.write_bytes(archive.extractfile(member).read())
            destination.chmod(0o755)


if __name__ == "__main__":
    main()
