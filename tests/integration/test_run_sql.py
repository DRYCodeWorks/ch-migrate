"""SQL files execute and render without changing literal values."""

import subprocess
import sys

import pytest

pytestmark = pytest.mark.integration


def test_run_sql_multiple_statements(project):
    (project.sql_dir / "case.sql").write_text(
        "CREATE TABLE IF NOT EXISTS {db}.logs (id UInt64) ENGINE = MergeTree ORDER BY id;\n"
        "ALTER TABLE {db}.logs ADD COLUMN IF NOT EXISTS value String;\n"
        "-- ch-migrate: allow-non-idempotent Test fixture inserts once into a fresh table\n"
        "INSERT INTO {db}.logs VALUES (1, 'a;b');\n"
    )
    _revision(project)
    result = project.run("up", "it")
    assert result.exit_code == 0, result.output
    assert project.client.query(
        "SELECT name FROM system.columns WHERE database = {db:String} AND table = 'logs' "
        "ORDER BY position",
        parameters={"db": project.database},
    ).result_rows == [("id",), ("value",)]
    assert project.client.query(f"SELECT id, value FROM {project.database}.logs").result_rows == [
        (1, "a;b")
    ]


def test_run_sql_colons_and_percent_literals(project):
    (project.sql_dir / "case.sql").write_text(_literal_sql())
    _revision(project)
    result = project.run("up", "it")
    assert result.exit_code == 0, result.output
    year = project.client.query("SELECT formatDateTime(now(), '%Y')").result_rows[0][0]
    assert project.client.query(
        f"SELECT id, value FROM {project.database}.logs ORDER BY id"
    ).result_rows == [
        (1, '{"a":1}'),
        (2, ":abc"),
        (3, "a%b"),
        (4, year),
        (5, "comment"),
    ]


def test_run_sql_offline_preserves_literals(project, monkeypatch):
    (project.sql_dir / "case.sql").write_text(_literal_sql())
    _revision(project)
    monkeypatch.setenv("CH_ENVIRONMENT", "it")
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=project.root,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    for statement in _literal_sql(waivers=False).replace("{db}", project.database).split(";\n"):
        if statement.strip():
            assert statement.strip() + ";" in result.stdout, result.stdout
    assert project.client.command(f"EXISTS TABLE {project.database}.logs") == 0


def test_run_sql_stops_on_first_failure(project):
    (project.sql_dir / "case.sql").write_text(
        "CREATE TABLE IF NOT EXISTS {db}.logs (id UInt64) ENGINE = MergeTree ORDER BY id;\n"
        "-- ch-migrate: allow-non-idempotent Test deliberately exercises a missing destination\n"
        "INSERT INTO {db}.missing VALUES (1);\n"
        "-- ch-migrate: allow-non-idempotent Test verifies this insert never runs after failure\n"
        "INSERT INTO {db}.logs VALUES (1);\n"
    )
    _revision(project)
    result = project.run("up", "it")
    assert result.exit_code != 0
    # The report points at the failing statement and ClickHouse's error, not a traceback.
    assert "migrations/sql/case.sql (statement 2 of 3, line 3)" in result.output
    assert "UNKNOWN_TABLE" in result.output
    assert "Traceback" not in result.output
    assert project.client.command(f"SELECT count() FROM {project.database}.logs") == 0
    assert (
        project.client.query(
            f"SELECT version_num FROM {project.database}.alembic_version"
        ).result_rows
        == []
    )


def _revision(project):
    project.write_revision(
        "sql_001", {"upgrade": "from ch_migrate import run_sql\nrun_sql('case.sql')"}
    )


def _literal_sql(waivers=True):
    waiver = (
        "-- ch-migrate: allow-non-idempotent Test fixture loads literals once\n" if waivers else ""
    )
    return (
        "CREATE TABLE IF NOT EXISTS {db}.logs (id UInt64, value String) ENGINE = MergeTree ORDER BY id;\n"
        + waiver
        + "INSERT INTO {db}.logs VALUES (1, '{\"a\":1}'), (2, ':abc'), (3, 'a%b'), "
        "(4, formatDateTime(now(), '%Y'));\n"
        + waiver
        + "INSERT /* see :ref */ INTO {db}.logs VALUES (5, 'comment');\n"
    )
