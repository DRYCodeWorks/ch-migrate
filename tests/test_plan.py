"""Plan JSON error behavior and schema's consumer-visible constraints."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from jsonschema import Draft202012Validator, ValidationError

from ch_migrate.cli import main

SCHEMA_PATH = Path(__file__).parents[1] / "docs/schemas/plan.schema.json"


@pytest.fixture
def plan_project(tmp_path, monkeypatch):
    initialized = CliRunner().invoke(main, ["init", str(tmp_path)])
    assert initialized.exit_code == 0, initialized.output
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def validator():
    schema = json.loads(SCHEMA_PATH.read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


@pytest.mark.parametrize("environment", ["missing", "dev"])
def test_plan_configuration_failure_is_a_schema_valid_error(plan_project, validator, environment):
    result = CliRunner().invoke(main, ["plan", environment, "--json"])
    assert result.exit_code == 2, result.output
    document = json.loads(result.stdout)
    validator.validate(document)
    assert document["schema_version"] == 1
    assert document["command"] == "plan"
    assert isinstance(document["error"], str) and document["error"]
    assert "migrations" not in document


def test_plan_schema_rejects_false_precision_and_missing_migration_fields(validator):
    document = {
        "schema_version": 1,
        "command": "plan",
        "database": "example",
        "environment": "dev",
        "gate_would_refuse": False,
        "warnings": [],
        "findings": [],
        "counts": {
            "total": 0,
            "error": 0,
            "warning": 0,
            "info": 0,
            "blocking": 0,
            "waived": 0,
        },
        "migrations": [],
    }
    validator.validate(document)
    with pytest.raises(ValidationError):
        validator.validate({**document, "gate_would_refuse": "false"})
    with pytest.raises(ValidationError):
        validator.validate({**document, "migrations": [{"revision": "aaaa"}]})
    with pytest.raises(ValidationError):
        validator.validate({**document, "counts": {**document["counts"], "blocking": -1}})


def test_plan_unknown_environment_does_not_suppress_error_with_empty_success(plan_project):
    result = CliRunner().invoke(main, ["plan", "not-configured"])
    assert result.exit_code != 0
    assert "not-configured" in result.output or "environment" in result.output.lower()
