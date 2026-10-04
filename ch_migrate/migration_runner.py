"""Own the upgrade process so SIGKILL cannot leave a child advancing revisions."""

from __future__ import annotations

import os
from pathlib import Path

from alembic import command
from alembic.config import Config


def run_upgrade(environment: str, revision: str, timeout: float | None) -> None:
    """Run Alembic directly: progress streams live and the CLI owns the waiter."""
    config = Config(str(Path.cwd() / "alembic.ini"))
    config.attributes["ch_migrate_environment"] = environment
    config.attributes["ch_migrate_command"] = "upgrade"
    config.attributes["ch_migrate_timeout"] = timeout
    # Project helpers intentionally read these during migration execution only.
    saved = {name: os.environ.get(name) for name in ("CH_DATABASE", "CH_CLUSTER")}
    try:
        command.upgrade(config, revision)
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
