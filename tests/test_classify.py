"""Corpus classifications and uncertain or quoted SQL boundaries."""

from pathlib import Path

import pytest
import yaml

from ch_migrate.classify import classify
from ch_migrate.introspect import Schema, parse_create_table
from ch_migrate.statements import MigrationStatement

CORPUS = yaml.safe_load((Path(__file__).parent / "corpus/classification.yaml").read_text())
VALUES = {"db": "example", "table": "example.sample"}


@pytest.fixture
def schema():
    table = parse_create_table(CORPUS["table_ddl"].format(**VALUES))
    return Schema(database="example", tables={"sample": table})


@pytest.mark.parametrize("case", CORPUS["statements"], ids=lambda case: case["id"])
def test_classification_corpus(case, schema):
    sql = case["sql"].format(**VALUES)
    result = classify(sql, schema)
    assert result.kind == case["expected"], (sql, result)
    if "detail" in case:
        assert case["detail"] in result.detail
    if sql.startswith("ALTER"):
        assert result.table == "example.sample"


@pytest.mark.parametrize(
    "case",
    [case for case in CORPUS["statements"] if "without_schema" in case],
    ids=lambda case: case["id"],
)
def test_classification_without_live_schema_is_conservative(case):
    result = classify(case["sql"].format(**VALUES))
    assert result.kind == case["without_schema"]
    if "MODIFY COLUMN" in case["sql"]:
        assert "if the type changes" in result.detail


def test_classification_ignores_comments_and_literal_keywords(schema):
    statement = MigrationStatement(
        "/* UPDATE x */ ALTER TABLE \"example\".`sample` ON CLUSTER 'local' "
        "MODIFY COLUMN x COMMENT 'DROP COLUMN y, MODIFY ENGINE = Memory'",
        "migration.sql",
        9,
        (),
        "upgrade",
    )
    result = classify(statement, schema)
    assert result.kind == "metadata"
    assert result.table == "example.sample"


def test_classification_different_database_cannot_supply_a_type(schema):
    result = classify("ALTER TABLE elsewhere.sample MODIFY COLUMN x UInt64", schema)
    assert result.kind == "mutation"
    assert "if the type changes" in result.detail


def test_classification_unknown_action_is_not_metadata(schema):
    result = classify("ALTER TABLE example.sample SOMETHING UNRECOGNIZED", schema)
    assert result.kind == "other"


def test_classification_preserves_identifier_case(schema):
    result = classify("ALTER TABLE example.sample MODIFY ORDER BY ID", schema)
    assert result.kind == "rebuild"


def test_classification_empty_and_read_only_have_no_table():
    assert classify("/* nothing */").kind == "other"
    assert classify("SELECT 'ALTER TABLE x DELETE WHERE 1'").table is None
