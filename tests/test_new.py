"""Authoring produces runnable revisions and rejects conflicts before writing."""

import ast
import re

import pytest
from click.testing import CliRunner

from ch_migrate import IrreversibleMigration
from ch_migrate.authoring import SqlFiles, read_revision_header, render_revision
from ch_migrate.cli import main
from ch_migrate.lint import lint_migrations
from ch_migrate.mv_validate import validate_mv_migrations


@pytest.fixture
def root(tmp_path, monkeypatch):
    result = CliRunner().invoke(main, ["init", str(tmp_path), "--name", "demo"])
    assert result.exit_code == 0, result.output
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CH_DEV_MIGRATION_PASSWORD", "test-only")
    return tmp_path


@pytest.mark.parametrize(
    "options,directory",
    [
        (["--table", "logs"], "tables/logs"),
        (["--view", "active_logs"], "views/active_logs"),
        (["--dict", "lookup"], "dictionaries/lookup"),
        ([], "other"),
    ],
)
def test_new_sql_paths_and_revision(root, options, directory):
    _new("add_status", options)
    [revision] = list((root / "migrations" / "versions").glob("*.py"))
    metadata = _assignments(revision.read_text())
    sql_root = root / "migrations" / "sql"
    files = sorted(sql_root.rglob("*.sql"))
    assert {path.parent for path in files} == {sql_root / "history" / directory}
    assert {path.name.rsplit(".", 2)[-2] for path in files} == {"up", "down"}
    for path in files:
        assert re.fullmatch(
            rf"\d{{4}}_\d{{2}}_\d{{2}}_\d{{4}}_{metadata['revision']}_add_status\.(up|down)\.sql",
            path.name,
        )
    tree = ast.parse(revision.read_text())
    paths = {
        call.args[0].value
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "run_sql"
    }
    assert paths == {str(path.relative_to(sql_root)) for path in files}
    assert metadata["down_revision"] is None


def test_new_consecutive_revisions_chain(root):
    _new("first", [])
    [first_path] = list((root / "migrations" / "versions").glob("*.py"))
    first = _assignments(first_path.read_text())["revision"]
    _new("second", [])
    second_path = next(
        p for p in (root / "migrations" / "versions").glob("*.py") if p != first_path
    )
    assert _assignments(second_path.read_text())["down_revision"] == first


def test_new_message_slug_is_bounded(root):
    _new("Long title! " + "x" * 70, [])
    [upgrade] = list((root / "migrations" / "sql").rglob("*.up.sql"))
    assert upgrade.name.endswith(("long_title_" + "x" * 29) + ".up.sql")


def test_new_irreversible_has_only_upgrade_and_backstop(root):
    _new("drop_legacy", ["--table", "logs", "--irreversible", "Drops legacy data"])
    sql_root = root / "migrations" / "sql"
    assert len(list(sql_root.rglob("*.up.sql"))) == 1
    assert list(sql_root.rglob("*.down.sql")) == []
    [path] = list((root / "migrations" / "versions").glob("*.py"))
    namespace = {}
    exec(compile(path.read_text(), str(path), "exec"), namespace)
    with pytest.raises(IrreversibleMigration) as raised:
        namespace["downgrade"]()
    assert raised.value.revision == namespace["revision"]
    assert raised.value.reason == "Drops legacy data"


@pytest.mark.parametrize(
    "options",
    [
        ["--table", "logs", "--view", "v"],
        ["--table", "logs", "--dict", "d"],
        ["--view", "v", "--dict", "d"],
        ["--table", "logs", "--view", "v", "--dict", "d"],
        ["--exchange"],
        ["--exchange", "--table", "logs", "--python"],
        ["--irreversible", "reason", "--python"],
        ["--irreversible", "reason", "--exchange", "--table", "logs"],
        ["--irreversible", ""],
        ["--irreversible", "   "],
    ],
)
def test_new_invalid_options_leave_no_artifacts(root, options):
    result = CliRunner().invoke(main, ["new", "dev", "invalid", *options])
    assert result.exit_code == 1, result.output
    assert list((root / "migrations" / "versions").glob("*.py")) == []
    assert list((root / "migrations" / "sql").rglob("*.sql")) == []


@pytest.mark.parametrize("options,sql_count", [([], 0), (["--table", "logs"], 1)])
def test_new_python_preserves_legacy_authoring(root, options, sql_count):
    _new("legacy", ["--python", *options])
    [path] = list((root / "migrations" / "versions").glob("*.py"))
    tree = ast.parse(path.read_text())
    imports = {
        alias.name for node in tree.body if isinstance(node, ast.ImportFrom) for alias in node.names
    }
    assert {"get_db", "read_sql"} <= imports
    assert "run_sql" not in imports
    sql_files = list((root / "migrations" / "sql").rglob("*.sql"))
    assert len(sql_files) == sql_count
    assert all(not p.name.endswith((".up.sql", ".down.sql")) for p in sql_files)


def test_new_lint_and_mv_validation_discover_run_sql(root):
    _new("drop_old", [])
    [upgrade] = list((root / "migrations" / "sql").rglob("*.up.sql"))
    upgrade.write_text("DROP TABLE IF EXISTS {db}.old;\n")
    report = lint_migrations(root / "migrations" / "versions")
    assert any(result.rule == "destructive_changes" for result in report.results)
    result = CliRunner().invoke(main, ["lint"])
    assert result.exit_code == 0, result.output
    assert "destructive_changes" in result.output, result.output
    upgrade.write_text("CREATE MATERIALIZED VIEW {db}.mv TO {db}.dst AS SELECT id FROM {db}.src;")
    errors = validate_mv_migrations(root / "migrations" / "versions")
    assert any("MV_DECLARATIONS" in error.message for error in errors)


def test_rewrite_preserves_header_and_merge_metadata(tmp_path):
    path = tmp_path / "merge.py"
    original = (
        '"""Merge two branches\n\nRevision ID: merge\nRevises: a, b\nCreate Date: 2026-10-02\n"""\n'
        "revision = 'merge'\ndown_revision = ('a', 'b')\n"
        "branch_labels = ('topic',)\ndepends_on = ('dependency',)\n"
    )
    path.write_text(original)
    result = render_revision(read_revision_header(path), SqlFiles("up.sql", "down.sql"), None)
    assert ast.get_docstring(ast.parse(result)) == ast.get_docstring(ast.parse(original))
    assert _assignments(result) == _assignments(original)


def _new(name, options):
    result = CliRunner().invoke(main, ["new", "dev", name, *options])
    assert result.exit_code == 0, result.output


def _assignments(source):
    return {
        node.targets[0].id: ast.literal_eval(node.value)
        for node in ast.parse(source).body
        if isinstance(node, ast.Assign)
    }
