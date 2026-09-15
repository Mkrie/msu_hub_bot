"""Keep legacy imports local to the executable, not to package imports."""

import runpy
import sys
from pathlib import Path


def prepare_imports() -> None:
    app_dir = Path(__file__).resolve().parent.parent / "hub_bot"
    if str(app_dir) not in sys.path:
        sys.path.insert(0, str(app_dir))


def main() -> None:
    from msu_hub_bot.settings import settings

    settings.validate_core()
    from msu_hub_bot.redaction import install_redaction

    install_redaction()
    from msu_hub_bot.health import heartbeat_path

    heartbeat_path().unlink(missing_ok=True)
    prepare_imports()
    runpy.run_module("main", run_name="__main__")


if __name__ == "__main__":
    main()
