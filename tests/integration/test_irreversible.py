"""Refuse an entire irreversible downgrade range before changing the schema."""

import subprocess
import sys

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def irreversible_project(project):
    project.write_revision(
        "aaaa",
        {
            "upgrade": 'op.execute(f"CREATE TABLE {db}.logs '
            '(id UInt64, legacy String) ENGINE = Memory")',
            "downgrade": 'op.execute(f"DROP TABLE {db}.logs")',
        },
    )
    path = project.write_revision(
        "bbbb",
        {
            "upgrade": 'op.execute(f"ALTER TABLE {db}.logs DROP COLUMN legacy")',
            "downgrade": "from ch_migrate import IrreversibleMigration\n"
            "raise IrreversibleMigration(revision, irreversible)",
        },
        down_revision="aaaa",
    )
    path.write_text(path.read_text() + '\nirreversible = "Drops legacy data"\n')
    project.write_revision(
        "cccc",
        {
            "upgrade": 'op.execute(f"ALTER TABLE {db}.logs ADD COLUMN current String")',
            "downgrade": 'op.execute(f"ALTER TABLE {db}.logs DROP COLUMN current")',
        },
        down_revision="bbbb",
    )
    result = project.run("up", "it")
    assert result.exit_code == 0, result.output
    return project


def test_irreversible_down_preserves_head_and_schema(irreversible_project):
    project = irreversible_project
    result = project.run("down", "it")
    assert result.exit_code == 0, result.output
    assert _heads(project) == [("bbbb",)]
    before = project.client.command(f"SHOW CREATE TABLE {project.database}.logs")
    for args in (("down", "it"), ("down", "it", "-r", "base")):
        refused = project.run(*args)
        assert refused.exit_code == 1, refused.output
        assert "bbbb" in refused.output and "Drops legacy data" in refused.output
        assert "nothing was run" in refused.output
        assert _heads(project) == [("bbbb",)]
        assert project.client.command(f"SHOW CREATE TABLE {project.database}.logs") == before


def test_irreversible_range_refuses_before_reversible_child(irreversible_project):
    project = irreversible_project
    before = project.client.command(f"SHOW CREATE TABLE {project.database}.logs")
    refused = project.run("down", "it", "-r", "base")
    assert refused.exit_code == 1, refused.output
    assert "bbbb" in refused.output
    # The whole range is listed, so it is clear only bbbb blocks the downgrade.
    assert "Of the 3 migrations it would revert, 1 is irreversible" in refused.output
    assert "cccc" in refused.output and "(reversible)" in refused.output
    assert "ch-migrate down it -r bbbb" in refused.output
    assert _heads(project) == [("cccc",)]
    assert project.client.command(f"SHOW CREATE TABLE {project.database}.logs") == before


def test_irreversible_direct_alembic_backstop(irreversible_project, monkeypatch):
    project = irreversible_project
    result = project.run("down", "it")
    assert result.exit_code == 0, result.output
    assert _heads(project) == [("bbbb",)]
    monkeypatch.setenv("CH_ENVIRONMENT", "it")
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "-1"],
        cwd=project.root,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "IrreversibleMigration" in result.stderr
    assert "bbbb" in result.stderr and "Drops legacy data" in result.stderr
    assert _heads(project) == [("bbbb",)]


def _heads(project):
    return project.client.query(
        f"SELECT version_num FROM {project.database}.alembic_version"
    ).result_rows
