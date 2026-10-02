"""Versioned documents from actual commands against the owned ClickHouse fixture."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

pytestmark = pytest.mark.integration
SCHEMAS = Path(__file__).parents[2] / "docs" / "schemas"


def test_json_commands_follow_live_migration_and_drift_state(project):
    # One project deliberately follows pending -> applied -> drift transitions.
    project.write_revision(
        "aaaa",
        {
            "upgrade": 'op.execute(f"CREATE TABLE IF NOT EXISTS {db}.events (id UInt64) ENGINE = MergeTree ORDER BY id")'
        },
    )
    project.write_revision(
        "bbbb",
        {"upgrade": 'op.execute(f"ALTER TABLE {db}.events ADD COLUMN IF NOT EXISTS extra String")'},
        "aaaa",
    )
    result, pending = _document(project, "status")
    assert result.exit_code == 1 and pending["pending"] == ["aaaa", "bbbb"]
    result, lint = _document(project, "lint")
    assert result.exit_code == 0 and lint["counts"]["blocking"] == 0
    upgraded = project.run("up", "it")
    assert upgraded.exit_code == 0, upgraded.output
    result, status = _document(project, "status")
    assert result.exit_code == 0 and status["at_head"]
    assert status["current_heads"] == ["bbbb"]
    assert status["applied"] == ["aaaa", "bbbb"]
    result, history = _document(project, "history")
    assert result.exit_code == 0
    assert {item["revision"] for item in history["revisions"] if item["applied"]} == {
        "aaaa",
        "bbbb",
    }
    snapshot = project.run("snapshot", "it")
    assert snapshot.exit_code == 0, snapshot.output
    result, before = _document(project, "diff")
    assert result.exit_code == 0 and before["in_sync"], result.output
    project.client.command(
        f"ALTER TABLE {project.database}.events ADD COLUMN outside_migrations UInt32"
    )
    result, after = _document(project, "diff")
    assert result.exit_code == 1 and not after["in_sync"]
    event = next(item for item in after["objects"] if item["name"] == "events")
    assert event["status"] == "modified"
    assert any("outside_migrations" in detail["field"] for detail in event["details"])
    _jq_status(project)


def test_json_live_lint_exposes_waiver_and_blocking_statement(project):
    project.write_revision(
        "aaaa",
        {
            "upgrade": """
        # ch-migrate: allow-non-idempotent Operator reconciles this one-time insert
        op.execute(f"INSERT INTO {db}.events SELECT 1")
        op.execute(f"CREATE TABLE {db}.events (id UInt64) ENGINE = Memory")
    """
        },
    )
    result, document = _document(project, "lint")
    assert result.exit_code == 1
    findings = [item for item in document["findings"] if item["rule"] == "idempotency"]
    assert any(
        item["waived"] == "Operator reconciles this one-time insert" and not item["blocking"]
        for item in findings
    )
    assert any(item["blocking"] and item["waived"] is None for item in findings)


def _document(project, command):
    result = project.run(command, "it", "--json")
    document = json.loads(result.stdout)
    schema = json.loads((SCHEMAS / f"{command}.schema.json").read_text())
    Draft202012Validator(schema).validate(document)
    return result, document


def _jq_status(project):
    result = subprocess.run(
        [
            "bash",
            "-o",
            "pipefail",
            "-c",
            '"$1" -m ch_migrate.cli status it --json | jq -e .current_heads',
            "json-smoke",
            sys.executable,
        ],
        cwd=project.root,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["bbbb"]
