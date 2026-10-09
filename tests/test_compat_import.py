"""The pre-0.5 import name `clickhouse_alembic` keeps old projects running until 1.0."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path


def _run(tmp_path: Path, code: str) -> subprocess.CompletedProcess[str]:
    """Run code in a fresh interpreter so nothing is already in sys.modules."""
    script = tmp_path / "old_migration.py"
    script.write_text(textwrap.dedent(code))
    return subprocess.run(
        [sys.executable, "-W", "default", str(script)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_old_imports_resolve_to_the_same_modules_and_classes(tmp_path: Path) -> None:
    # The imports a 0.4 env.py and revision file contain.
    result = _run(
        tmp_path,
        """
        from clickhouse_alembic import IrreversibleMigration, run_sql
        from clickhouse_alembic.config import get_env_config
        import clickhouse_alembic.hooks as old_hooks
        import ch_migrate
        import ch_migrate.config
        import ch_migrate.hooks
        from ch_migrate.downgrade import IrreversibleMigration as New

        assert IrreversibleMigration is New
        assert run_sql is ch_migrate.run_sql
        assert get_env_config is ch_migrate.config.get_env_config
        assert old_hooks is ch_migrate.hooks
        print("ok")
        """,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_old_import_warns_at_the_importing_line(tmp_path: Path) -> None:
    result = _run(tmp_path, "import os\nfrom clickhouse_alembic import get_db\n")
    assert result.returncode == 0, result.stderr
    assert "old_migration.py:2: FutureWarning" in result.stderr
    assert "renamed to ch_migrate" in result.stderr
