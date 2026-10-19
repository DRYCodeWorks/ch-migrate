"""One checked session per run, explicit legacy upgrade, and preserved hook behavior."""

import re
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.integration
LEGACY_ENV = Path(__file__).parents[1] / "fixtures" / "env_v0_4_1.py"


@pytest.fixture
def legacy_project(project):
    (project.root / "migrations" / "env.py").write_bytes(LEGACY_ENV.read_bytes())
    (project.sql_dir / "first.sql").write_text(
        "CREATE TABLE IF NOT EXISTS {db}.legacy (id UInt64) ENGINE = Memory"
    )
    legacy_revision = project.write_revision(
        "aaaa",
        {
            "upgrade": "from clickhouse_alembic import read_sql\n"
            "op.execute(read_sql('first.sql', db=get_db()))"
        },
    )
    legacy_revision.write_text(
        legacy_revision.read_text().replace(
            "from ch_migrate import get_db", "from clickhouse_alembic import get_db"
        )
    )
    old = subprocess.run(
        [
            "uv",
            "tool",
            "run",
            "--isolated",
            "--python",
            "3.10",
            "--from",
            "clickhouse-alembic==0.4.1",
            "ch-migrate",
            "up",
            "it",
        ],
        cwd=project.root,
        capture_output=True,
        text=True,
    )
    assert old.returncode == 0, old.stdout + old.stderr
    assert project.client.query(
        "SELECT engine FROM system.tables WHERE database = {db:String} "
        "AND name = 'alembic_version'",
        parameters={"db": project.database},
    ).result_rows == [("ReplacingMergeTree",)]
    (project.sql_dir / "second.sql").write_text(
        "ALTER TABLE {db}.legacy ADD COLUMN IF NOT EXISTS value String"
    )
    project.write_revision(
        "bbbb",
        {
            "upgrade": "from ch_migrate import read_sql\n"
            "op.execute(read_sql('second.sql', db=get_db()))"
        },
        down_revision="aaaa",
    )
    return project


def test_session_sql_first_setting_applies(project):
    (project.sql_dir / "settings.sql").write_text(
        "CREATE TABLE IF NOT EXISTS {db}.probe (value UInt64) ENGINE = Memory;\n"
        "SET max_threads = 3;\n"
        "-- ch-migrate: allow-non-idempotent Test records the observed session setting once\n"
        "INSERT INTO {db}.probe SELECT getSetting('max_threads');\n"
    )
    project.write_revision(
        "aaaa", {"upgrade": "from ch_migrate import run_sql\nrun_sql('settings.sql')"}
    )
    result = project.run("up", "it")
    assert result.exit_code == 0, result.output
    assert project.client.query(f"SELECT value FROM {project.database}.probe").result_rows == [(3,)]


def test_session_setting_carries_across_revisions(project):
    project.write_revision(
        "aaaa",
        {
            "upgrade": 'op.execute(f"CREATE TABLE IF NOT EXISTS {db}.probe (value UInt64) ENGINE = Memory")\n'
            'op.execute("SET max_threads = 3")'
        },
    )
    project.write_revision(
        "bbbb",
        {
            "upgrade": "# ch-migrate: allow-non-idempotent Test records the next-revision setting once\n"
            "op.execute(f\"INSERT INTO {db}.probe SELECT getSetting('max_threads')\")"
        },
        down_revision="aaaa",
    )
    result = project.run("up", "it")
    assert result.exit_code == 0, result.output
    assert project.client.query(f"SELECT value FROM {project.database}.probe").result_rows == [(3,)]


def test_session_expiry_fails_instead_of_losing_settings(project):
    config = yaml.safe_load((project.root / "config.yaml").read_text())
    config["environments"]["it"]["session_timeout"] = 2
    (project.root / "config.yaml").write_text(yaml.safe_dump(config))
    project.write_revision(
        "aaaa",
        {
            "upgrade": "import time\n"
            'op.execute(f"CREATE TABLE IF NOT EXISTS {db}.probe (value UInt64) ENGINE = Memory")\n'
            'op.execute("SET max_threads = 3")\n'
            "time.sleep(4)\n"
            "# ch-migrate: allow-non-idempotent Probe must fail after session expiry\n"
            "op.execute(f\"INSERT INTO {db}.probe SELECT getSetting('max_threads')\")"
        },
    )
    result = project.run("up", "it")
    assert result.exit_code != 0
    assert "SESSION_NOT_FOUND" in result.output, result.output
    assert project.client.command(f"SELECT count() FROM {project.database}.probe") == 0
    assert (
        project.client.query(
            f"SELECT version_num FROM {project.database}.alembic_version"
        ).result_rows
        == []
    )


def test_upgrade_env_existing_release_and_idempotent_backup(legacy_project):
    project = legacy_project
    original = {path.name: path.read_bytes() for path in project.versions_dir.glob("*.py")}
    first = project.run("upgrade-env")
    assert first.exit_code == 0, first.output
    backup = project.root / "migrations" / "env.py.bak"
    assert backup.read_bytes() == LEGACY_ENV.read_bytes()
    second = project.run("upgrade-env")
    assert second.exit_code == 0, second.output
    assert backup.read_bytes() == LEGACY_ENV.read_bytes()
    applied = project.run("up", "it")
    assert applied.exit_code == 0, applied.output
    assert {path.name: path.read_bytes() for path in project.versions_dir.glob("*.py")} == original
    assert project.client.query(
        "SELECT name FROM system.columns WHERE database = {db:String} AND table = 'legacy' "
        "ORDER BY position",
        parameters={"db": project.database},
    ).result_rows == [("id",), ("value",)]
    status = project.run("status", "it")
    assert status.exit_code == 0, status.output
    assert re.search(r"Applied:\s+2\b", status.output), status.output
    assert re.search(r"Pending:\s+0\b", status.output), status.output


def test_upgrade_env_required_before_legacy_commands_connect(legacy_project):
    project = legacy_project
    before = project.client.command(f"SHOW CREATE TABLE {project.database}.legacy")
    for command in ("up", "down", "history"):
        refused = project.run(command, "it")
        assert refused.exit_code == 1, refused.output
        assert "Run `ch-migrate upgrade-env`." in refused.output
        assert "Can't load plugin" not in refused.output
        assert project.client.command(f"SHOW CREATE TABLE {project.database}.legacy") == before
        assert project.client.query(
            f"SELECT version_num FROM {project.database}.alembic_version"
        ).result_rows == [("aaaa",)]
    # Status remains a non-blocking reporter under the 0.5.1 contract.
    status = project.run("status", "it")
    assert status.exit_code == 0, status.output
    assert re.search(r"Pending:\s+1\b", status.output), status.output


def test_session_preserves_batch_and_revision_hooks(project):
    config = yaml.safe_load((project.root / "config.yaml").read_text())
    config["hooks"] = {
        "pre_migrate": [
            "CREATE TABLE IF NOT EXISTS {db}.hook_events (phase String) ENGINE = Memory",
            "INSERT INTO {db}.hook_events VALUES ('pre')",
        ],
        "post_migrate": ["INSERT INTO {db}.hook_events VALUES ('post')"],
    }
    (project.root / "config.yaml").write_text(yaml.safe_dump(config))
    project.write_revision("aaaa", {"upgrade": 'op.execute("SELECT 1")'})
    project.write_revision("bbbb", {"upgrade": 'op.execute("SELECT 2")'}, down_revision="aaaa")
    result = project.run("up", "it")
    assert result.exit_code == 0, result.output
    assert project.client.query(
        f"SELECT phase, count() FROM {project.database}.hook_events GROUP BY phase ORDER BY phase"
    ).result_rows == [("post", 2), ("pre", 1)]
