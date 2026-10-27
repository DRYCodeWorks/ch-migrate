"""Every corpus ALTER is classified against actual mutation or rejection evidence."""

from pathlib import Path

import pytest
import yaml
from clickhouse_connect.driver.exceptions import DatabaseError

from ch_migrate.classify import classify
from ch_migrate.introspect import get_live_schema

CORPUS = yaml.safe_load((Path(__file__).parents[1] / "corpus/classification.yaml").read_text())
ALTERS = [case for case in CORPUS["statements"] if case["sql"].startswith("ALTER")]
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("case", ALTERS, ids=lambda case: case["id"])
def test_classification_matches_real_alter_work(project, case):
    values = {"db": project.database, "table": f"{project.database}.sample"}
    project.client.command(CORPUS["table_ddl"].format(**values))
    project.client.command(CORPUS["seed"].format(**values))
    statement = case["sql"].format(**values)
    result = classify(statement, get_live_schema(project.client, project.database))
    assert result.kind == case["expected"], (statement, result)
    before = _mutation_ids(project)
    if result.kind == "rebuild":
        code = 36 if "sorting" in case["id"] else 62
        with pytest.raises(DatabaseError, match=f"code: {code},"):
            project.client.command(statement)
        assert _mutation_ids(project) == before
        return
    project.client.command(statement, settings={"mutations_sync": 0, "alter_sync": 0})
    created_mutations = _mutation_ids(project) - before
    assert bool(created_mutations) == (result.kind == "mutation"), (
        statement,
        result,
        created_mutations,
    )


def test_classification_lightweight_update_is_synchronous_patch_work(project):
    table = f"{project.database}.patches"
    project.client.command(
        f"CREATE TABLE {table} (id UInt64, x UInt64) ENGINE = MergeTree ORDER BY id "
        "SETTINGS enable_block_number_column = 1, enable_block_offset_column = 1"
    )
    project.client.command(f"INSERT INTO {table} VALUES (1, 10), (2, 20)")
    statement = f"UPDATE {table} SET x = x + 1 WHERE id = 1"
    result = classify(statement)
    project.client.command(statement, settings={"allow_experimental_lightweight_update": 1})
    assert project.client.query(f"SELECT id, x FROM {table} ORDER BY id").result_rows == [
        (1, 11),
        (2, 20),
    ]
    rows = project.client.query(
        "SELECT mutation_id FROM system.mutations WHERE database = {db:String} AND table = 'patches'",
        parameters={"db": project.database},
    ).result_rows
    assert result.kind == "other" and "lightweight" in result.detail
    assert rows == []


def _mutation_ids(project):
    rows = project.client.query(
        "SELECT DISTINCT mutation_id FROM system.mutations "
        "WHERE database = {db:String} AND table = 'sample'",
        parameters={"db": project.database},
    ).result_rows
    return {row[0] for row in rows}
