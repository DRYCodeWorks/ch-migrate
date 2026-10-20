"""Errors and warnings from `up`, its gate and `upgrade-env` use the `ui` markers.

`--json` callers must still get the error message unchanged, as plain text.
"""

import json
from types import SimpleNamespace

import click
import pytest
from click.testing import CliRunner

from ch_migrate import ui
from ch_migrate.cli import _print_gate_findings, main
from ch_migrate.lint import LintResult, Severity

OLD_ENV_MESSAGE = "This project's migrations/env.py is from ch-migrate 0.x. Run `ch-migrate upgrade-env`."


@pytest.fixture
def project(tmp_path, monkeypatch):
    result = CliRunner().invoke(main, ["init", str(tmp_path), "--name", "demo"])
    assert result.exit_code == 0, result.output
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CH_DEV_MIGRATION_PASSWORD", "test-only")
    return tmp_path


def test_failure_prints_marker_then_hints():
    @click.command()
    def broken():
        raise ui.Failure("It broke.", "Run `ch-migrate lint`.")

    result = CliRunner().invoke(broken)
    assert result.exit_code == 1
    assert result.stderr == "✗ It broke.\nRun ch-migrate lint.\n"


def test_failure_message_keeps_hints_for_json():
    failure = ui.Failure("It broke.", "Run `ch-migrate lint`.")
    assert failure.format_message() == "It broke. Run `ch-migrate lint`."


def test_old_env_refusal(project):
    (project / "migrations" / "env.py").write_text("# ch-migrate 0.5 env.py\n")
    result = CliRunner().invoke(main, ["up", "dev"])
    assert result.exit_code == 1
    assert result.stderr == (
        "✗ This project's migrations/env.py is from ch-migrate 0.x.\n"
        "Run ch-migrate upgrade-env.\n"
    )


def test_old_env_refusal_json_message_is_unchanged(project):
    (project / "migrations" / "env.py").write_text("# ch-migrate 0.5 env.py\n")
    result = CliRunner().invoke(main, ["history", "dev", "--json"])
    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"] == OLD_ENV_MESSAGE


def test_gate_findings_use_lint_markers(capsys):
    report = SimpleNamespace(
        results=[
            LintResult(
                rule="idempotency",
                message="Use IF NOT EXISTS",
                severity=Severity.ERROR,
                file="a.up.sql",
                line=2,
                statement="CREATE TABLE t",
            ),
            LintResult(
                rule="destructive_changes",
                message="Drops a column",
                severity=Severity.ERROR,
            ),
            LintResult(
                rule="idempotency",
                message="Idempotency waiver: copies once",
                severity=Severity.INFO,
                file="b.up.sql",
                line=5,
            ),
        ]
    )
    assert _print_gate_findings(report) is True
    captured = capsys.readouterr()
    assert (
        captured.err
        == "✗ a.up.sql:2  Use IF NOT EXISTS  [idempotency]\n  CREATE TABLE t\n"
    )
    assert captured.out == (
        "! config.yaml  Drops a column  [destructive_changes]\n"
        "✓ b.up.sql:5  Idempotency waiver: copies once  [idempotency]\n"
    )


def test_upgrade_env_marks_baseline_and_warning(project):
    result = CliRunner().invoke(main, ["upgrade-env"])
    assert result.exit_code == 0, result.output
    assert result.stdout == (
        "✓ Recorded gate baseline: (empty)\n"
        "✓ migrations/env.py is already current; backup unchanged.\n"
    )
    assert result.stderr.startswith(
        "! Existing version tables are not converted.\n"
        "If the deployment is replicated, run ch-migrate status <env> and follow README"
    )
