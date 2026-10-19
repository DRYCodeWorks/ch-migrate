"""ClickHouse Cloud-only checks; they skip on self-hosted servers (DRY-1413)."""

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.cloud]

PROBES = 50
PROBE_GAP_SECONDS = 1.25


def test_cloud_session_routing_keeps_set_for_every_statement(project):
    """A migration's `SET` must reach every later statement, whichever replica serves it."""
    db = project.database
    statements = [
        (
            "CREATE TABLE IF NOT EXISTS {db}.routing (seq UInt16, max_threads String, "
            "host String, at DateTime64(6)) ENGINE = MergeTree ORDER BY seq"
        ),
        "SET max_threads = 3",
    ]
    for seq in range(1, PROBES + 1):
        statements.append(
            "-- ch-migrate: allow-non-idempotent Session-routing probe row\n"
            f"INSERT INTO {{db}}.routing SELECT {seq}, getSetting('max_threads'), "
            "hostName(), now64(6)"
        )
        statements.append(f"SELECT sleep({PROBE_GAP_SECONDS})")
    (project.sql_dir / "routing.sql").write_text(";\n".join(statements) + ";\n")
    project.write_revision(
        "aaaa", {"upgrade": "from ch_migrate import run_sql\nrun_sql('routing.sql')"}
    )
    result = project.run("up", "it")
    assert result.exit_code == 0, result.output
    probes, wrong, replicas, span = project.client.query(
        f"SELECT count(), countIf(max_threads != '3'), uniqExact(host), "
        f"dateDiff('millisecond', min(at), max(at)) / 1000 FROM {db}.routing",
        settings={"select_sequential_consistency": 1},
    ).result_rows[0]
    print(
        f"\nSESSION_ROUTING probes={probes} wrong_max_threads={wrong} "
        f"replicas={replicas} span_seconds={span:.1f}"
    )
    assert probes == PROBES and wrong == 0
    assert span >= 60


def test_cloud_version_table_is_shared_and_round_trips(project):
    project.write_revision("aaaa", {"upgrade": 'op.execute("SELECT 1")'})
    project.write_revision("bbbb", {"upgrade": 'op.execute("SELECT 1")'}, down_revision="aaaa")
    for command, head in (("up", "bbbb"), ("down", "aaaa"), ("up", "bbbb")):
        result = project.run(command, "it")
        assert result.exit_code == 0, result.output
        heads = project.client.query(
            "SELECT DISTINCT version_num FROM clusterAllReplicas('default', "
            f"{project.database}.alembic_version)",
            settings={"skip_unavailable_shards": 1},
        ).result_rows
        assert heads == [(head,)], command
    engine = project.client.command(
        "SELECT engine FROM system.tables WHERE database = {db:String} AND name = 'alembic_version'",
        parameters={"db": project.database},
    )
    assert engine == "SharedMergeTree"
