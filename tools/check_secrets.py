"""Scan reviewed files and Git history; report only redacted finding locations."""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    executable = os.environ.get("GITLEAKS_BIN", "gitleaks")
    paths = (
        subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT, capture_output=True, check=True)
        .stdout.decode()
        .split("\0")
    )
    with tempfile.TemporaryDirectory(prefix="msu-hub-scan-") as directory:
        directory = Path(directory)
        tree = directory / "tree"
        tree.mkdir()
        for rel in set(filter(None, paths)):
            path = ROOT / rel
            if not path.is_file():
                continue
            if path.name.startswith(".env") and path.name != ".env.example":
                raise SystemExit("A real environment file is in the source manifest")
            if path.suffix in {".dump", ".rdb", ".key", ".pem"} or path.name.startswith("core."):
                raise SystemExit("A private runtime artifact is in the source manifest")
            target = tree / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
        commands = [["dir", str(tree)]]
        if subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=ROOT, capture_output=True).returncode == 0:
            commands.append(["git", str(ROOT), "--log-opts=--all"])
        findings = []
        for index, command in enumerate(commands):
            report = directory / f"report-{index}.json"
            result = subprocess.run(
                [
                    executable,
                    *command,
                    "--config",
                    str(ROOT / ".gitleaks.toml"),
                    "--redact",
                    "--no-banner",
                    "--report-format",
                    "json",
                    "--report-path",
                    str(report),
                ],
                capture_output=True,
            )
            if result.returncode not in {0, 1} or not report.exists():
                raise SystemExit("Secret scanner failed to run")
            for finding in json.loads(report.read_text()):
                filename = finding["File"]
                if filename.startswith(str(tree)):
                    filename = str(Path(filename).relative_to(tree))
                findings.append({"file": filename, "line": finding["StartLine"], "rule": finding["RuleID"]})
        if findings:
            print(json.dumps(findings, indent=2))
            raise SystemExit(1)
    print("Source and available Git history passed secret scanning")


if __name__ == "__main__":
    main()
