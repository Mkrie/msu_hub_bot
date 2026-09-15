"""Send a deployment request over verified SSH without logging configuration."""

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

PUBLIC_STATUS = {
    "Image pulled; checking configuration and connections",
    "Preflight passed; stopping the current poller",
    "Release failed; restoring the previous poller",
    "Previous poller restored",
    "Rollback completed",
    "Rollback failed; restoring the current release",
}


def report_result(result):
    # SSH diagnostics can expose resolved IPs or host paths in public Actions logs.
    for line in result.stdout.splitlines():
        if line in PUBLIC_STATUS or re.fullmatch(r"Deployed [0-9a-f]{40} ghcr\.io/uburuntu/msu_hub_bot@sha256:[0-9a-f]{64}", line):
            print(line)
    if result.returncode:
        print("Deployment failed; inspect the host privately for details", file=sys.stderr)


def main():
    host = os.environ["DEPLOY_HOST"]
    user = os.environ["DEPLOY_USER"]
    port = int(os.environ.get("DEPLOY_PORT", "22"))
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:-]*", host) or not re.fullmatch(r"[a-z_][a-z0-9_-]*", user) or not 1 <= port <= 65535:
        raise SystemExit("Invalid SSH connection settings")
    operation = os.environ.get("DEPLOY_OPERATION", "deploy")
    payload = {"action": operation}
    if operation == "deploy":
        payload.update(
            image=os.environ["DEPLOY_IMAGE"],
            revision=os.environ["GITHUB_SHA"],
            environment={key: value for key, value in os.environ.items() if key.startswith("HUB_") and value},
            registry_username=os.environ["GITHUB_ACTOR"],
            registry_token=os.environ["GH_TOKEN"],
        )
    with tempfile.TemporaryDirectory(prefix="msu-hub-ssh-") as directory:
        directory = Path(directory)
        key = directory / "key"
        key.write_text(os.environ["DEPLOY_SSH_KEY"].strip() + "\n")
        key.chmod(0o600)
        known_hosts = directory / "known_hosts"
        known_hosts.write_text(os.environ["DEPLOY_KNOWN_HOSTS"].strip() + "\n")
        command = [
            "ssh",
            "-T",
            "-i",
            str(key),
            "-p",
            str(port),
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "UserKnownHostsFile=" + str(known_hosts),
            "-o",
            "ConnectTimeout=20",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=3",
            user + "@" + host,
            "msu-hub-bot",
        ]
        print("Connecting to deployment host", flush=True)
        result = subprocess.run(command, input=json.dumps(payload), text=True, capture_output=True)
        report_result(result)
        raise SystemExit(result.returncode)


if __name__ == "__main__":
    try:
        main()
    except (KeyError, ValueError, OSError):
        print("Deployment connection configuration is incomplete or invalid", file=sys.stderr)
        raise SystemExit(1)
