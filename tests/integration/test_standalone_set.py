"""A legacy SET migration cannot run until the session-safe environment is installed."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


def test_standalone_set_requires_upgrade_env_then_preserves_setting(project):
    legacy = Path(__file__).parents[1] / "fixtures/env_v0_4_1.py"
    (project.root / "migrations/env.py").write_bytes(legacy.read_bytes())
    (project.sql_dir / "set.sql").write_text(
        "CREATE TABLE IF NOT EXISTS {db}.probe (value UInt64) ENGINE = Memory;\n"
        "SET max_threads = 3;\n"
        "-- ch-migrate: allow-non-idempotent Test captures the session value once\n"
        "INSERT INTO {db}.probe SELECT getSetting('max_threads');\n"
    )
    project.write_revision(
        "aaaa", {"upgrade": "from ch_migrate import run_sql\nrun_sql('set.sql')"}
    )
    lint = project.run("lint")
    assert lint.exit_code == 1, lint.output
    assert "standalone_set" in lint.output
    refused = project.run("up", "it")
    assert refused.exit_code == 1, refused.output
    assert "upgrade-env" in refused.output
    assert project.client.command(f"EXISTS TABLE {project.database}.probe") == 0
    assert project.client.command(f"EXISTS TABLE {project.database}.alembic_version") == 0
    upgraded = project.run("upgrade-env")
    assert upgraded.exit_code == 0, upgraded.output
    applied = project.run("up", "it")
    assert applied.exit_code == 0, applied.output
    assert project.client.query(f"SELECT value FROM {project.database}.probe").result_rows == [(3,)]
