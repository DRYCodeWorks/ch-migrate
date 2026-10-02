"""One shard, two replicas: replication and distributed-DDL catch-up are real."""

import time

import pytest
from clickhouse_connect.driver.exceptions import DatabaseError

pytestmark = [pytest.mark.integration, pytest.mark.cluster]


def test_harness_replicates_rows_and_finishes_ddl(cluster_project, clickhouse_cluster):
    project, cluster = cluster_project, clickhouse_cluster
    table = f"{project.database}.replicated"
    cluster.clients[1].command(
        f"CREATE TABLE {table} ON CLUSTER {cluster.name} (id UInt64) "
        f"ENGINE = ReplicatedMergeTree('/clickhouse/tables/{project.database}/replicated', '{{replica}}') "
        "ORDER BY id"
    )
    assert cluster.clients[2].command(f"EXISTS TABLE {table}") == 1
    cluster.clients[1].command(f"INSERT INTO {table} VALUES (42)")
    cluster.clients[2].command(f"SYSTEM SYNC REPLICA {table}")
    assert cluster.clients[2].query(f"SELECT id FROM {table}").result_rows == [(42,)]
    _wait_finished(cluster, project.database, "replicated")
    rows = _queue_rows(cluster, project.database, "replicated")
    assert {host: (status, code) for host, status, code in rows} == {
        cluster.hostnames[1]: ("Finished", 0),
        cluster.hostnames[2]: ("Finished", 0),
    }


def test_harness_stopped_node_leaves_ddl_pending_then_catches_up(
    cluster_project, clickhouse_cluster
):
    project, cluster = cluster_project, clickhouse_cluster
    table = f"{project.database}.delayed"
    cluster.stop_node(2)
    try:
        with pytest.raises(DatabaseError, match="TIMEOUT_EXCEEDED|UNFINISHED"):
            cluster.clients[1].command(
                f"CREATE TABLE {table} ON CLUSTER {cluster.name} (id UInt64) ENGINE = Memory",
                # Buffer the error instead of losing it as an incomplete chunked response.
                settings={"distributed_ddl_task_timeout": 5, "wait_end_of_query": 1},
            )
        rows = _queue_rows(cluster, project.database, "delayed")
        states = {host: status for host, status, _ in rows}
        assert states[cluster.hostnames[1]] == "Finished"
        assert states[cluster.hostnames[2]] != "Finished"
    finally:
        cluster.start_node(2)
    _wait_finished(cluster, project.database, "delayed")
    assert cluster.clients[2].command(f"EXISTS TABLE {table}") == 1


def _queue_rows(cluster, database, table):
    return (
        cluster.clients[1]
        .query(
            "SELECT host, status, exception_code FROM system.distributed_ddl_queue "
            "WHERE startsWith(query, 'CREATE TABLE') AND position(query, {db:String}) > 0 "
            "AND position(query, {table:String}) > 0 ORDER BY host",
            parameters={"db": database, "table": table},
        )
        .result_rows
    )


def _wait_finished(cluster, database, table):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        rows = _queue_rows(cluster, database, table)
        if len(rows) == 2 and all(status == "Finished" and code == 0 for _, status, code in rows):
            return
        time.sleep(0.1)
    pytest.fail(f"Distributed DDL did not finish on both owned replicas: {rows}")
