"""Execute the documented workflow's actual shell steps on an owned server."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

pytestmark = pytest.mark.integration
REPO = Path(__file__).parents[2]
EXAMPLE = REPO / "docs/examples/github-actions-drift.yml"


@pytest.mark.parametrize("drift", [False, True], ids=["in_sync", "out_of_band_change"])
def test_drift_workflow_run_steps(project, drift):
    project.client.command(
        f"CREATE TABLE {project.database}.events (id UInt64) ENGINE = MergeTree ORDER BY id"
    )
    captured = project.run("snapshot", "it")
    assert captured.exit_code == 0, captured.output
    snapshot = next((project.sql_dir / "snapshots").iterdir())
    if drift:
        project.client.command(
            f"ALTER TABLE {project.database}.events ADD COLUMN unexpected UInt32"
        )
    outcomes = _run_steps(project, _workflow_environment(project, snapshot))
    for name in ("install", "credentials", "cleanup"):
        assert outcomes[name].returncode == 0, outcomes[name].stderr
    assert outcomes["compare"].returncode == int(drift), outcomes["compare"].stderr
    assert outcomes["gate"].returncode == int(drift)
    document = json.loads((project.root / "drift.json").read_text())
    schema = json.loads((REPO / "docs/schemas/diff.schema.json").read_text())
    Draft202012Validator(schema).validate(document)
    assert document["in_sync"] is (not drift)
    events = next(item for item in document["objects"] if item["name"] == "events")
    assert events["status"] == ("modified" if drift else "in_sync")
    assert not (project.root / ".env.local").exists()


def test_drift_workflow_passes_actionlint():
    validator = shutil.which("actionlint")
    if validator:
        command = [validator, str(EXAMPLE)]
    elif shutil.which("docker"):
        command = ["docker", "run", "--rm", "-i", "rhysd/actionlint:1.7.7", "-"]
    else:
        pytest.skip("Neither actionlint nor Docker is available to validate the workflow")
    result = subprocess.run(
        command, input=EXAMPLE.read_text(), capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _workflow_environment(project, snapshot):
    environment = os.environ.copy()
    password = environment.pop("CH_IT_MIGRATION_PASSWORD")
    environment.update(
        {
            "CH_MIGRATE_PACKAGE": str(REPO),
            "CH_MIGRATE_PYTHON": sys.executable,
            "CH_MIGRATE_ENV": "it",
            "CH_MIGRATE_SNAPSHOT_DIR": str(snapshot),
            "DRIFT_MIGRATION_PASSWORD": password,
            "UV_TOOL_DIR": str(project.root / "tools"),
            "UV_TOOL_BIN_DIR": str(project.root / "bin"),
        }
    )
    return environment


def _run_steps(project, environment):
    steps = yaml.safe_load(EXAMPLE.read_text())["jobs"]["drift"]["steps"]
    outcomes = {}
    for step in steps:
        if "run" not in step:
            continue
        if step["id"] == "gate":
            environment["DRIFT_OUTCOME"] = (
                "success" if outcomes["compare"].returncode == 0 else "failure"
            )
        outcomes[step["id"]] = subprocess.run(
            ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", step["run"]],
            cwd=project.root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
        )
    return outcomes
