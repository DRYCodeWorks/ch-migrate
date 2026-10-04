"""Plan acceptance against live owned ClickHouse objects and revision state."""

import json
import secrets
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

pytestmark = pytest.mark.integration
REPO = Path(__file__).parents[2]
CORPUS = yaml.safe_load((REPO / "tests/corpus/classification.yaml").read_text())


def test_plan_classifies_every_pending_corpus_statement(project):
    database = project.database
    table = f"{database}.sample"
    project.client.command(CORPUS["table_ddl"].format(db=database, table=table))
    project.client.command(CORPUS["seed"].format(db=database, table=table))
    project.client.command("SYSTEM FLUSH LOGS")  # Seed observable writer history, outside plan.
    sql = "\n".join(
        case["sql"].format(db=database, table=table) + ";" for case in CORPUS["statements"]
    )
    (project.sql_dir / "corpus.sql").write_text(sql + "\n")
    project.write_revision(
        "corpus",
        {"upgrade": "from ch_migrate import run_sql\nrun_sql('corpus.sql')"},
    )
    result, document = _plan(project)
    assert result.exit_code == 1, result.output  # Unwaived corpus actions block up.
    assert document["gate_would_refuse"] is True
    statements = document["migrations"][0]["statements"]
    assert [item["sql"].strip().rstrip(";") for item in statements] == [
        case["sql"].format(db=database, table=table) for case in CORPUS["statements"]
    ]
    for case, statement in zip(CORPUS["statements"], statements):
        assert statement["classification"]["kind"] == case["expected"], case["id"]
        assert statement["line"] >= 1 and statement["file"].endswith("corpus.sql")
        if "detail" in case:
            assert case["detail"] in statement["classification"]["detail"]
    assert project.client.command(f"EXISTS TABLE {database}.alembic_version") == 0


def test_plan_orders_pending_branches_after_applied_ancestors(project):
    _revision(
        project, "root", None, "CREATE TABLE IF NOT EXISTS {db}.root (id UInt64) ENGINE = Memory"
    )
    _revision(
        project, "left", "root", "CREATE TABLE IF NOT EXISTS {db}.left (id UInt64) ENGINE = Memory"
    )
    _revision(
        project,
        "right",
        "root",
        "CREATE TABLE IF NOT EXISTS {db}.right (id UInt64) ENGINE = Memory",
    )
    _revision(
        project,
        "merged",
        ("left", "right"),
        "CREATE TABLE IF NOT EXISTS {db}.merged (id UInt64) ENGINE = Memory",
    )
    applied = project.run("up", "it", "-r", "right")
    assert applied.exit_code == 0, applied.output
    result, document = _plan(project)
    assert result.exit_code == 0, result.output
    assert [item["revision"] for item in document["migrations"]] == ["left", "merged"]
    assert all(len(item["statements"]) == 1 for item in document["migrations"])
    assert all(
        item["statements"][0]["sql"].startswith("CREATE TABLE") for item in document["migrations"]
    )
    assert document["gate_would_refuse"] is False
    assert project.client.command(f"EXISTS TABLE {project.database}.left") == 0


def test_plan_reports_exact_column_bytes_and_table_and_partition_ceilings(project):
    table = f"{project.database}.events"
    project.client.command(
        f"CREATE TABLE {table} (id UInt64, x UInt64, payload String, ts Date) "
        "ENGINE = MergeTree PARTITION BY toYYYYMM(ts) ORDER BY id"
        " SETTINGS min_rows_for_wide_part = 0, min_bytes_for_wide_part = 0"
    )
    for month in (1, 2):
        project.client.command(
            f"INSERT INTO {table} VALUES ({month}, {month}, "
            f"'{('row' * (month * 200))}', '2020-{month:02d}-01')"
        )
    sql = (
        f"ALTER TABLE {table} MODIFY COLUMN x UInt32;\n"
        f"ALTER TABLE {table} DROP COLUMN payload;\n"
        f"ALTER TABLE {table} UPDATE x = x + 1 WHERE id = 1;\n"
        f"ALTER TABLE {table} UPDATE x = x + 1 IN PARTITION ID '202001' WHERE id = 1;\n"
    )
    (project.sql_dir / "sizes.sql").write_text(sql)
    project.write_revision(
        "sizes", {"upgrade": "from ch_migrate import run_sql\nrun_sql('sizes.sql')"}
    )
    result, document = _plan(project)
    assert result.exit_code == 1, result.output
    sizes = [item["size"] for item in document["migrations"][0]["statements"]]
    assert sizes[0]["precision"] == sizes[1]["precision"] == "exact"
    assert sizes[0]["columns"] == ["x"]
    assert sizes[1]["columns"] == ["payload"]
    _assert_size(sizes[0], _column_stats(project, "events", "x"))
    _assert_size(sizes[1], _column_stats(project, "events", "payload"))
    assert sizes[2]["precision"] == sizes[3]["precision"] == "ceiling"
    assert sizes[2]["partition_id"] is None
    assert sizes[3]["partition_id"] == "202001"
    _assert_size(sizes[2], _column_stats(project, "events"))
    _assert_size(sizes[3], _column_stats(project, "events", partition="202001"))
    human = project.run("plan", "it")
    assert human.exit_code == result.exit_code
    for value in ("events", "sizes.sql", "exact", "ceiling", "x", "payload"):
        assert value.lower() in human.output.lower(), human.output
    assert "up to" in human.output
    for size in sizes:
        for field in ("compressed_bytes", "uncompressed_bytes", "part_count"):
            assert str(size[field]) in human.output
    assert "202001" in human.output


def test_plan_compact_column_bytes_are_a_ceiling_not_zero_work(project):
    table = f"{project.database}.compact"
    project.client.command(
        f"CREATE TABLE {table} (id UInt64, x UInt64) ENGINE = MergeTree ORDER BY id "
        "SETTINGS min_rows_for_wide_part = 1000000, min_bytes_for_wide_part = 100000000"
    )
    project.client.command(f"INSERT INTO {table} VALUES (1, 2)")
    _revision(project, "compact", None, f"ALTER TABLE {table} MODIFY COLUMN x UInt32")
    result, document = _plan(project)
    assert result.exit_code == 0, result.output
    size = document["migrations"][0]["statements"][0]["size"]
    assert size["precision"] == "ceiling"
    assert size["columns"] == ["x"]
    _assert_size(size, _column_stats(project, "compact"))


@pytest.mark.parametrize(
    ("predicate", "partition"),
    [
        ("toYYYYMM(ts) = 202001", "202001"),
        ("_partition_id = '202001'", "202001"),
        ("toYYYYMM(ts) = 202001 OR id = 2", None),
    ],
)
def test_plan_narrows_only_proven_partition_predicates(project, predicate, partition):
    table = f"{project.database}.events"
    project.client.command(
        f"CREATE TABLE {table} (id UInt64, ts Date) ENGINE = MergeTree PARTITION BY toYYYYMM(ts) ORDER BY id"
    )
    project.client.command(f"INSERT INTO {table} VALUES (1, '2020-01-01'), (2, '2020-02-01')")
    _revision(project, "partition", None, f"DELETE FROM {table} WHERE {predicate}")
    result, document = _plan(project)
    assert result.exit_code == 1, result.output
    size = document["migrations"][0]["statements"][0]["size"]
    assert size["precision"] == "ceiling"
    assert size["partition_id"] == partition
    _assert_size(size, _column_stats(project, "events", partition=partition))


def test_plan_reports_actual_mv_and_dictionary_dependents(project):
    database = project.database
    source = f"{database}.source"
    project.client.command(
        f"CREATE TABLE {source} (id UInt64, value UInt64) ENGINE = MergeTree ORDER BY id"
    )
    project.client.command(
        f"CREATE TABLE {database}.sink (id UInt64) ENGINE = MergeTree ORDER BY id"
    )
    project.client.command(
        f"CREATE MATERIALIZED VIEW {database}.source_mv TO {database}.sink "
        f"AS SELECT id FROM {source}"
    )
    project.client.command(
        f"CREATE DICTIONARY {database}.source_dict (id UInt64, value UInt64) "
        "PRIMARY KEY id "
        f"SOURCE(CLICKHOUSE(DB '{database}' TABLE 'source')) "
        "LIFETIME(MIN 0 MAX 0) LAYOUT(FLAT())"
    )
    _revision(project, "deps", None, f"ALTER TABLE {source} DROP COLUMN IF EXISTS value")
    result, document = _plan(project)
    assert result.exit_code == 0, result.output
    statement = document["migrations"][0]["statements"][0]
    dependencies = {(item["name"].split(".")[-1], item["type"]) for item in statement["downstream"]}
    assert ("source_mv", "materialized_view") in dependencies
    assert ("source_dict", "dictionary") in dependencies
    human = project.run("plan", "it")
    assert human.exit_code == 0, human.output
    for name in ("source_mv", "source_dict", "DROP COLUMN", "deps"):
        assert name in human.output


def test_plan_gate_only_blocks_gate_rules_and_reports_all_findings(project):
    table = f"{project.database}.events"
    project.client.command(f"CREATE TABLE {table} (id UInt64) ENGINE = MergeTree ORDER BY id")
    path = project.sql_dir / "gate.sql"
    path.write_text(
        f"DROP TABLE IF EXISTS {table};\n"
        f"CREATE TABLE {table} (id UInt64) ENGINE = MergeTree ORDER BY id;\n"
    )
    project.write_revision(
        "gate", {"upgrade": "from ch_migrate import run_sql\nrun_sql('gate.sql')"}
    )
    result, document = _plan(project)
    assert result.exit_code == 1, result.output
    assert document["gate_would_refuse"] is True
    assert document["counts"]["blocking"] >= 1
    findings = document["findings"]
    assert {item["rule"] for item in findings} >= {"idempotency", "destructive_changes"}
    assert any(item["rule"] == "idempotency" and item["blocking"] for item in findings)
    assert all(not item["blocking"] for item in findings if item["rule"] == "destructive_changes")
    assert any(item["file"].endswith("gate.sql") and item["line"] == 2 for item in findings)
    assert any(
        item["rule"] == "idempotency"
        for item in document["migrations"][0]["statements"][1]["findings"]
    )
    human = project.run("plan", "it")
    assert human.exit_code == 1
    assert "idempotency" in human.output and "destructive_changes" in human.output


def test_plan_under_read_only_user_emits_only_inspection_queries(project, monkeypatch):
    database = project.database
    table = f"{database}.events"
    project.client.command(
        f"CREATE TABLE {table} (id UInt64, x UInt64) ENGINE = MergeTree ORDER BY id"
    )
    project.client.command(f"INSERT INTO {table} VALUES (1, 5)")
    _revision(project, "inspect", None, f"ALTER TABLE {table} MODIFY COLUMN x UInt32")
    user = "plan_audit_" + secrets.token_hex(6)
    password = secrets.token_hex(24)
    project.client.command(f"CREATE USER {user} IDENTIFIED WITH sha256_password BY '{password}'")
    try:
        project.client.command(f"GRANT SELECT, SHOW TABLES, SHOW DATABASES ON *.* TO {user}")
        config_path = project.root / "config.yaml"
        config = yaml.safe_load(config_path.read_text())
        config["environments"]["it"]["migration_user"] = user
        config_path.write_text(yaml.safe_dump(config))
        monkeypatch.setenv("CH_IT_MIGRATION_PASSWORD", password)
        result, document = _plan(project)
        assert result.exit_code == 0, result.output
        assert document["migrations"][0]["statements"][0]["classification"]["kind"] == "mutation"
        assert project.client.command(f"EXISTS TABLE {database}.alembic_version") == 0
        project.client.command("SYSTEM FLUSH LOGS")  # Audit observer; not the plan user.
        rows = project.client.query(
            "SELECT DISTINCT query_kind FROM system.query_log "
            "WHERE type = 'QueryFinish' AND user = {audit_user:String}",
            parameters={"audit_user": user},
        ).result_rows
        assert rows, "Plan user has no QueryFinish entries; read-only audit is inconclusive"
        assert {row[0] for row in rows} <= {"Select", "Show", "Describe", "Explain"}, rows
    finally:
        project.client.command(f"DROP USER IF EXISTS {user}")


def _plan(project):
    result = project.run("plan", "it", "--json")
    document = json.loads(result.stdout)
    schema = json.loads((REPO / "docs/schemas/plan.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(document)
    assert document["command"] == "plan" and document["schema_version"] == 1
    if "error" not in document:
        assert document["database"] == project.database
        assert document["environment"] == "it"
    return result, document


def _revision(project, revision, parent, sql):
    sql = sql.replace("{db}", project.database)
    project.write_revision(
        revision, {"upgrade": f"from alembic import op\nop.execute({sql!r})"}, parent
    )


def _column_stats(project, table, column=None, partition=None):
    clauses = ["database = {db:String}", "table = {table:String}", "active = 1"]
    params = {"db": project.database, "table": table}
    if column is not None:
        clauses.append("column = {column:String}")
        params["column"] = column
    if partition is not None:
        clauses.append("partition_id = {partition:String}")
        params["partition"] = partition
    columns = (
        "sum(column_data_compressed_bytes), sum(column_data_uncompressed_bytes), uniqExact(name)"
    )
    source = "system.parts_columns"
    if column is None:
        columns = "sum(data_compressed_bytes), sum(data_uncompressed_bytes), count()"
        source = "system.parts"
    rows = project.client.query(
        f"SELECT {columns} FROM {source} WHERE " + " AND ".join(clauses), parameters=params
    ).result_rows
    return tuple(int(value) for value in rows[0])


def _assert_size(actual, expected):
    assert actual["compressed_bytes"] == expected[0]
    assert actual["uncompressed_bytes"] == expected[1]
    assert actual["part_count"] == expected[2]
