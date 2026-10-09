"""Statement boundaries, origin information, and revision scope for lint."""

from pathlib import Path

import pytest

from ch_migrate.lint import lint_migrations
from ch_migrate.rebase import build_revision_graph
from ch_migrate.statements import migration_statements, pending_revisions


@pytest.fixture
def project(tmp_path):
    (tmp_path / "migrations/versions").mkdir(parents=True)
    (tmp_path / "migrations/sql").mkdir()
    return tmp_path


def test_sql_first_files_keep_direction_line_comments_and_placeholders(project):
    (project / "migrations/sql/up.sql").write_text(
        "-- waiver: intentional\nDROP TABLE IF EXISTS {db}.old;\n\nSELECT 2;\n"
    )
    (project / "migrations/sql/down.sql").write_text("DROP TABLE {db}.current;")
    path = _revision(
        project,
        "def upgrade():\n    run_sql('up.sql')\n" "def downgrade():\n    run_sql('down.sql')\n",
    )
    statements = migration_statements(path)
    assert [(s.sql, s.source, s.line, s.direction) for s in statements] == [
        ("DROP TABLE IF EXISTS {db}.old", "migrations/sql/up.sql", 2, "upgrade"),
        ("SELECT 2", "migrations/sql/up.sql", 4, "upgrade"),
        ("DROP TABLE {db}.current", "migrations/sql/down.sql", 1, "downgrade"),
    ]
    assert statements[0].comments == ("waiver: intentional",)
    assert statements[1].comments == ()


def test_python_literals_and_fstrings_are_static_and_keep_call_lines(project):
    path = _revision(
        project,
        "raise RuntimeError('must not import')\n"
        "def upgrade():\n"
        "    # python reason\n"
        "    op.execute('-- sql reason\\nDROP TABLE {db}.first; SELECT 2')\n"
        "    op.execute(f'ALTER TABLE {db}.second ADD COLUMN value String')\n"
        "def downgrade():\n    op.execute('DROP TABLE {db}.second')\n",
    )
    statements = migration_statements(path)
    assert [s.sql for s in statements] == [
        "DROP TABLE {db}.first",
        "SELECT 2",
        "ALTER TABLE {db}.second ADD COLUMN value String",
        "DROP TABLE {db}.second",
    ]
    assert [s.line for s in statements] == [6, 6, 7, 9]
    assert statements[0].comments == ("python reason", "sql reason")
    assert statements[1].comments == ()
    assert {s.source for s in statements} == {"migrations/versions/a.py"}


def test_read_sql_nested_in_execute_is_collected_once(project):
    (project / "migrations/sql/file.sql").write_text("SELECT 1;\nSELECT 2")
    path = _revision(project, "def upgrade():\n    op.execute(read_sql('file.sql', db=get_db()))\n")
    statements = migration_statements(path)
    assert [(s.sql, s.line) for s in statements] == [("SELECT 1", 1), ("SELECT 2", 2)]


def test_comment_blank_line_breaks_python_attachment(project):
    path = _revision(
        project, "def upgrade():\n    # unrelated\n\n    op.execute('DROP TABLE old')\n"
    )
    assert migration_statements(path)[0].comments == ()


def test_fstring_conversion_and_format_are_not_evaluated(project):
    path = _revision(project, "def upgrade():\n    op.execute(f'SELECT {value!r}, {number:04d}')\n")
    assert migration_statements(path)[0].sql == "SELECT {value!r}, {number:04d}"


def test_lint_does_not_report_downgrade_sql(project):
    _revision(
        project,
        "def upgrade():\n    op.execute('SELECT 1')\n"
        "def downgrade():\n    op.execute('DROP TABLE dangerous')\n",
    )
    assert lint_migrations(project / "migrations/versions").results == []


def test_lint_reports_sql_file_and_statement_start(project):
    (project / "migrations/sql/file.sql").write_text(
        "SELECT 1;\n\n-- reason\nDROP TABLE IF EXISTS old;\n"
    )
    _revision(project, "def upgrade():\n    run_sql('file.sql')\n")
    report = lint_migrations(project / "migrations/versions")
    assert [(r.rule, r.file, r.line) for r in report.results] == [
        ("destructive_changes", "migrations/sql/file.sql", 4)
    ]


def test_explicit_empty_revision_scope_does_not_lint_everything(project):
    _revision(project, "def upgrade():\n    op.execute('DROP TABLE old')\n")
    versions = project / "migrations/versions"
    assert lint_migrations(versions, revisions=set()).results == []
    assert lint_migrations(versions, revisions={"a"}).warning_count == 2


def test_pending_scope_expands_applied_ancestors_and_keeps_other_branch(project):
    versions = project / "migrations/versions"
    for revision, parent in (("a", None), ("b", "a"), ("c", "a")):
        (versions / f"{revision}.py").write_text(
            f"revision = {revision!r}\ndown_revision = {parent!r}\n"
        )
    graph = build_revision_graph(versions)
    assert pending_revisions(graph, {"b"}) == {"c"}
    assert pending_revisions(graph, {"b", "c"}) == set()
    assert pending_revisions(graph, set()) == {"a", "b", "c"}
    with pytest.raises(ValueError, match="missing locally"):
        pending_revisions(graph, {"not_here"})


def test_missing_referenced_file_is_not_silently_ignored(project):
    path = _revision(project, "def upgrade():\n    run_sql('missing.sql')\n")
    with pytest.raises(FileNotFoundError):
        migration_statements(path)


def _revision(project: Path, body: str) -> Path:
    path = project / "migrations/versions/a.py"
    path.write_text("revision = 'a'\ndown_revision = None\n" + body)
    return path
