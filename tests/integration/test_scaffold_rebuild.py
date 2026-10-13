"""Fetch live DDL, edit only SQL, plan it and execute the generated guarded revision."""

import json
import shlex
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

pytestmark = pytest.mark.integration
SCHEMA = Path(__file__).parents[2] / "docs" / "schemas" / "plan.schema.json"


def test_scaffold_rebuild_round_trip_uses_only_sql_edits(project):
    table = f"{project.database}.t"
    project.client.command(
        f"CREATE TABLE {table} (id UInt64, k UInt8, value String) ENGINE = MergeTree ORDER BY id"
    )
    project.client.command(
        f"INSERT INTO {table} SELECT number, number % 7, toString(number) FROM numbers(1000)"
    )
    before = project.client.query(
        f"SELECT count(), sum(cityHash64(id, k, value)) FROM {table}"
    ).result_rows[0]
    project.write_revision("aaaa", {"upgrade": "op.execute('SELECT 1')"})
    baseline = project.run("up", "it")
    assert baseline.exit_code == 0, baseline.output
    created = project.run("new", "it", "reorder", "--table", "t", "--rebuild")
    assert created.exit_code == 0, created.output
    [sql_file] = list((project.sql_dir / "history" / "tables" / "t").glob("*.up.sql"))
    assert list(project.sql_dir.rglob("*.down.sql")) == []
    source = sql_file.read_text()
    assert "{db}.`t`" in source
    assert project.database not in source
    assert "ORDER BY id" in source
    sql_file.write_text(source.replace("ORDER BY id", "ORDER BY (k, id)", 1))
    project.client.command("SYSTEM FLUSH LOGS")
    planned = project.run("plan", "it", "--json")
    assert planned.exit_code == 0, planned.output
    document = json.loads(planned.stdout)
    Draft202012Validator(json.loads(SCHEMA.read_text())).validate(document)
    statement = document["migrations"][0]["statements"][0]
    assert statement["classification"]["kind"] == "rebuild"
    assert shlex.split(statement["suggested_command"]) == [
        "ch-migrate",
        "new",
        "it",
        "rebuild_t",
        "--table",
        "t",
        "--rebuild",
    ]
    assert document["schema_version"] == 1
    human = project.run("plan", "it")
    assert human.exit_code == 0, human.output
    assert statement["suggested_command"] in human.output
    refusals = {
        item["code"] for item in statement["rebuild"]["findings"] if item["severity"] == "refusal"
    }
    # The scaffold names the Cloud declaration as False; Cloud refuses until it is True.
    assert refusals == ({"cloud_writers_undeclared"} if project.cloud else set())
    if project.cloud:
        _assert_cloud_refusal(project, project.run("up", "it", "--timeout", "30"), before)
        [revision] = project.versions_dir.glob("*reorder*.py")
        revision.write_text(
            revision.read_text().replace(
                "writers_on_this_service_only=False", "writers_on_this_service_only=True"
            )
        )
    applied = project.run("up", "it", "--timeout", "180")
    assert applied.exit_code == 0, applied.output
    assert "ORDER BY (k, id)" in project.client.command(f"SHOW CREATE TABLE {table}")
    assert (
        project.client.query(
            f"SELECT count(), sum(cityHash64(id, k, value)) FROM {table}"
        ).result_rows[0]
        == before
    )
    refused = project.run("down", "it")
    assert refused.exit_code != 0
    assert "irreversible" in refused.output.lower()
    assert (
        project.client.query(
            f"SELECT count(), sum(cityHash64(id, k, value)) FROM {table}"
        ).result_rows[0]
        == before
    )


def _assert_cloud_refusal(project, applied, before):
    """Undeclared Cloud writers: refused before any helper, lock or swap exists."""
    assert applied.exit_code != 0, applied.output
    assert "cloud_writers_undeclared" in applied.output
    table = f"{project.database}.t"
    assert "ORDER BY id" in project.client.command(f"SHOW CREATE TABLE {table}")
    assert (
        project.client.query(
            f"SELECT count(), sum(cityHash64(id, k, value)) FROM {table}"
        ).result_rows[0]
        == before
    )
    leftovers = project.client.query(
        "SELECT name FROM system.tables WHERE database = {db:String} AND name LIKE '%chm%'",
        parameters={"db": project.database},
    ).result_rows
    assert leftovers == []
