"""A generated SQL-first migration needs SQL edits, not Python edits."""

import ast

import pytest

pytestmark = pytest.mark.integration


def test_sql_first_generated_revision_applies_and_reverts(project):
    created = project.run("new", "it", "add_status", "--table", "logs")
    assert created.exit_code == 0, created.output
    [revision] = list(project.versions_dir.glob("*.py"))
    original_revision = revision.read_text()
    [upgrade] = list(project.sql_dir.rglob("*.up.sql"))
    [downgrade] = list(project.sql_dir.rglob("*.down.sql"))
    upgrade.write_text(
        "CREATE TABLE IF NOT EXISTS {db}.logs (id UInt64) ENGINE = MergeTree ORDER BY id;\n"
        "ALTER TABLE {db}.logs ADD COLUMN IF NOT EXISTS status String;\n"
    )
    downgrade.write_text("DROP TABLE IF EXISTS {db}.logs;\n")
    applied = project.run("up", "it")
    assert applied.exit_code == 0, applied.output
    assert project.client.query(
        f"SELECT name FROM system.columns WHERE database = '{project.database}' "
        "AND table = 'logs' ORDER BY position"
    ).result_rows == [("id",), ("status",)]
    reverted = project.run("down", "it")
    assert reverted.exit_code == 0, reverted.output
    assert project.client.command(f"EXISTS TABLE {project.database}.logs") == 0
    assert revision.read_text() == original_revision


def test_sql_first_exchange_still_generates_legacy_scaffold(project):
    # MergeTree, not Memory: Memory rows stay on the writing replica of a multi-replica server.
    project.client.command(
        f"CREATE TABLE {project.database}.logs (id UInt64) ENGINE = MergeTree ORDER BY id"
    )
    project.client.command(f"INSERT INTO {project.database}.logs VALUES (7)")
    created = project.run("new", "it", "rebuild_logs", "--table", "logs", "--exchange")
    assert created.exit_code == 0, created.output
    assert list(project.sql_dir.rglob("*.up.sql")) == []
    [sql_file] = list(project.sql_dir.rglob("*.sql"))
    assert sql_file.parent.name == "logs"
    [revision] = list(project.versions_dir.glob("*.py"))
    metadata = {
        node.targets[0].id: ast.literal_eval(node.value)
        for node in ast.parse(revision.read_text()).body
        if isinstance(node, ast.Assign)
    }
    assert metadata["irreversible"]
    applied = project.run("up", "it")
    assert applied.exit_code == 0, applied.output
    assert project.client.query(f"SELECT id FROM {project.database}.logs").result_rows == [(7,)]
    assert project.client.command(f"EXISTS TABLE {project.database}.logs") == 1
    assert project.client.command(f"EXISTS TABLE {project.database}.logs_shadow") == 0
