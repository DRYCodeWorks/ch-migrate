"""Guarded rebuild extraction and the migration gate's public behavior."""

from pathlib import Path

import pytest

from ch_migrate.classify import classify
from ch_migrate.lint import GATE_RULES, lint_migrations
from ch_migrate.statements import migration_statements


@pytest.fixture
def rebuild_project(tmp_path):
    versions = tmp_path / "migrations" / "versions"
    versions.mkdir(parents=True)
    sql = tmp_path / "migrations" / "sql"
    sql.mkdir()
    (sql / "replacement.sql").write_text(
        "CREATE TABLE {db}.events (id UInt64, value UInt64) ENGINE = MergeTree ORDER BY value;\n"
    )
    return versions


def test_rebuild_keeps_operation_order_sql_origin_and_options(rebuild_project):
    revision = _revision(
        rebuild_project,
        """
def upgrade():
    op.execute('SELECT 1')
    op.rebuild_table('events', 'replacement.sql', select='id, value', allow_unacknowledged_async_loss=True)
    op.execute('SELECT 2')
""",
    )
    statements = migration_statements(revision)
    assert statements[0].sql == "SELECT 1"
    assert statements[2].sql == "SELECT 2"
    rebuild = statements[1]
    assert rebuild.source == "migrations/sql/replacement.sql"
    assert rebuild.line == 1
    assert rebuild.rebuild.table == "events"
    assert rebuild.rebuild.select == "id, value"
    assert rebuild.rebuild.allow_unacknowledged_async_loss is True
    assert classify(rebuild).kind == "rebuild"
    assert classify(rebuild).table == "events"


def test_only_guarded_rebuild_is_exempt_from_create_idempotency(rebuild_project):
    _revision(
        rebuild_project,
        """
def upgrade():
    op.rebuild_table('events', 'replacement.sql')
    op.execute('CREATE TABLE unsafe (id UInt64) ENGINE = Memory')
""",
    )
    report = lint_migrations(rebuild_project)
    blockers = [item for item in report.results if item.rule in GATE_RULES]
    assert len(blockers) == 1
    assert blockers[0].file == "migrations/versions/a.py"
    assert "CREATE TABLE unsafe" in blockers[0].statement


def test_exported_helper_keyword_arguments_are_extracted(rebuild_project):
    revision = _revision(
        rebuild_project,
        """
def upgrade():
    rebuild_table(table=f'{db}.events', create_sql_path='replacement.sql')
""",
    )
    statement = migration_statements(revision)[0]
    assert statement.rebuild.table == "{db}.events"
    assert statement.rebuild.allow_unacknowledged_async_loss is False


@pytest.mark.parametrize("argument", ["unknown_path", "choose_path()"])
def test_unreadable_rebuild_definition_is_not_silently_omitted(rebuild_project, argument):
    revision = _revision(
        rebuild_project, f"def upgrade():\n    op.rebuild_table('events', {argument})\n"
    )
    with pytest.raises(ValueError, match="statically readable"):
        migration_statements(revision)


def test_rebuild_rejects_multiple_statements_before_gate_exemption(rebuild_project):
    path = rebuild_project.parent / "sql" / "replacement.sql"
    path.write_text(path.read_text() + "DROP TABLE another;\n")
    revision = _revision(
        rebuild_project, "def upgrade():\n    op.rebuild_table('events', 'replacement.sql')\n"
    )
    with pytest.raises(ValueError, match="exactly one"):
        migration_statements(revision)


def _revision(versions: Path, body: str) -> Path:
    path = versions / "a.py"
    path.write_text("revision = 'a'\ndown_revision = None\n" + body)
    return path
