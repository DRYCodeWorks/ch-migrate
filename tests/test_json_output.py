"""CLI JSON contracts: graph meaning, error exits, waivers, and structural drift."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner
from jsonschema import Draft202012Validator

from ch_migrate.cli import main
from ch_migrate.connection import MigrationState
from ch_migrate.introspect import Schema, parse_create_statement
from ch_migrate.version_table import VersionTableState

SCHEMAS = Path(__file__).parents[1] / "docs" / "schemas"


@pytest.fixture
def json_project(tmp_path, monkeypatch):
    runner = CliRunner()
    assert runner.invoke(main, ["init", str(tmp_path)]).exit_code == 0
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "ch_migrate.cli.get_env_config", lambda *args: {"database": "example"}
    )
    state = {"heads": set(), "error": None}

    def read_state(config):
        if state["error"]:
            raise OSError(state["error"])
        return MigrationState(state["heads"], VersionTableState("example", "Atomic"))

    monkeypatch.setattr("ch_migrate.connection.get_migration_state", read_state)
    return tmp_path, state


@pytest.fixture
def revision_project(json_project):
    root, state = json_project
    versions = root / "migrations" / "versions"
    _revision(versions / "root.py", None, 'irreversible = "Removed historical rows"')
    _revision(versions / "left.py", "root")
    _revision(versions / "right.py", "root")
    _revision(versions / "merged.py", ("left", "right"))
    return root, state


@pytest.mark.parametrize(
    "heads,applied,pending,exit_code",
    [
        (set(), [], ["left", "merged", "right", "root"], 1),
        ({"merged"}, ["left", "merged", "right", "root"], [], 0),
        ({"right"}, ["right", "root"], ["left", "merged"], 1),
        ({"foreign"}, [], ["left", "merged", "right", "root"], 1),
        ({"root", "merged"}, ["left", "merged", "right", "root"], [], 1),
    ],
)
def test_status_json_resolves_graph(revision_project, heads, applied, pending, exit_code):
    # Pytest's parameterization is the public test contract, not a runtime API.
    revision_project[1]["heads"] = heads
    result, document = _run("status", "dev", "--json")
    assert result.exit_code == exit_code, result.output
    assert document["current_heads"] == sorted(heads)
    assert document["script_heads"] == ["merged"]
    assert document["applied"] == applied
    assert document["pending"] == pending
    assert document["at_head"] is (exit_code == 0)
    assert document["database"] == "example"


def test_history_json_preserves_merge_and_irreversibility(revision_project):
    revision_project[1]["heads"] = {"merged"}
    result, document = _run("history", "dev", "--json")
    assert result.exit_code == 0
    revisions = {item["revision"]: item for item in document["revisions"]}
    assert revisions["merged"]["down_revisions"] == ["left", "right"]
    assert revisions["root"]["irreversible"] == "Removed historical rows"
    assert revisions["left"]["irreversible"] is None
    assert all(item["applied"] is True for item in revisions.values())
    assert revisions["root"]["description"] == "Migration root"
    assert revisions["root"]["create_date"] == "2026-10-02 12:00:00"
    assert Path(revisions["root"]["path"]).name == "root.py"


@pytest.mark.parametrize("command", ["status", "history"])
def test_revision_json_database_error_is_not_empty_success(revision_project, command):
    revision_project[1]["error"] = "database unavailable"
    result, document = _run(command, "dev", "--json")
    assert result.exit_code == 2
    assert "database unavailable" in document["error"]
    if command == "history":
        assert all(item["applied"] is None for item in document["revisions"])
    else:
        assert "at_head" not in document
    human = CliRunner().invoke(main, [command, "dev"])
    assert human.exit_code == 0


def test_history_json_unknown_head_does_not_claim_unapplied(revision_project):
    revision_project[1]["heads"] = {"foreign"}
    result, document = _run("history", "dev", "--json")
    assert result.exit_code == 0
    assert all(item["applied"] is None for item in document["revisions"])
    assert "foreign" in result.stderr


@pytest.mark.parametrize("command", ["status", "history", "lint", "diff"])
def test_json_config_errors_are_documents(json_project, monkeypatch, command):
    def fail_config(*args):
        raise ValueError("environment is not configured")

    monkeypatch.setattr("ch_migrate.cli.get_env_config", fail_config)
    result, document = _run(command, "missing", "--json")
    assert result.exit_code == (1 if command == "lint" else 2)
    assert "environment is not configured" in document["error"]


def test_lint_json_reports_gate_and_waiver_reason(json_project):
    root, _ = json_project
    sql = root / "migrations" / "sql" / "sample.up.sql"
    sql.write_text(
        "CREATE TABLE unsafe (id UInt64) ENGINE = MergeTree ORDER BY id;\n"
        "-- ch-migrate: allow-non-idempotent Copy is manually reconciled before retry\n"
        "INSERT INTO unsafe SELECT 1;\n"
    )
    _revision(
        root / "migrations" / "versions" / "sample.py",
        None,
        "from ch_migrate import run_sql\ndef upgrade():\n    run_sql('sample.up.sql')\n",
    )
    result, document = _run("lint", "--json")
    assert result.exit_code == 1
    gate = [item for item in document["findings"] if item["rule"] == "idempotency"]
    assert [(item["blocking"], item["waived"]) for item in gate] == [
        (True, None),
        (False, "Copy is manually reconciled before retry"),
    ]
    assert [item["line"] for item in gate] == [1, 3]
    assert all(item["file"].endswith("sample.up.sql") for item in gate)
    assert document["counts"]["blocking"] == 1
    assert document["counts"]["waived"] == 1
    assert document["counts"]["error"] == 1
    assert CliRunner().invoke(main, ["lint"]).exit_code == result.exit_code


@pytest.mark.parametrize("drift", [False, True])
def test_diff_json_reports_structural_changes(json_project, monkeypatch, drift):
    root, _ = json_project
    snapshot = root / "snapshot" / "tables"
    snapshot.mkdir(parents=True)
    before = "CREATE TABLE example.events (id UInt64) ENGINE = MergeTree ORDER BY id"
    (snapshot / "events.sql").write_text(before)
    live = Schema(database="example")
    after = before.replace("id UInt64", "id UInt64, extra String") if drift else before
    live.tables["events"] = parse_create_statement(after)
    client = MagicMock()
    monkeypatch.setattr("ch_migrate.connection.get_client", lambda config: client)
    monkeypatch.setattr("ch_migrate.introspect.get_live_schema", lambda *args: live)
    result, document = _run("diff", "dev", "--json", "--snapshot-dir", str(snapshot.parent))
    assert result.exit_code == int(drift), result.output
    assert document["in_sync"] is (not drift)
    item = document["objects"][0]
    assert item["name"] == "events" and item["type"] == "table"
    assert item["status"] == ("modified" if drift else "in_sync")
    if drift:
        assert any("extra" in detail["field"] for detail in item["details"])
    else:
        assert item["details"] == []


def test_diff_json_no_snapshot_returns_error(json_project):
    result, document = _run("diff", "dev", "--json")
    assert result.exit_code == 2
    assert "snapshot" in document["error"].lower()
    assert "in_sync" not in document


def test_json_invalid_option_value_is_parseable(json_project):
    result, document = _run("diff", "dev", "--json", "--snapshot-dir", "missing")
    assert result.exit_code == 2
    assert "missing" in document["error"]


def _revision(path, parents, body=""):
    path.write_text(
        f'"""Migration {path.stem}\n\nCreate Date: 2026-10-02 12:00:00\n"""\n'
        f"revision = {path.stem!r}\ndown_revision = {parents!r}\n{body}\n"
    )


def _run(*args):
    result = CliRunner().invoke(main, list(args))
    document = json.loads(result.stdout)
    schema = json.loads((SCHEMAS / f"{args[0]}.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(document)
    return result, document
