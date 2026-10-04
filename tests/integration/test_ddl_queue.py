"""Owned distributed DDL waits, failure propagation, and queue-identity recovery."""

import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager

import pytest

pytestmark = pytest.mark.integration


def test_ddl_queue_create_then_alter_finishes_every_host(cluster_project, clickhouse_cluster):
    project, cluster = cluster_project, clickhouse_cluster
    _baseline(project)
    _revision(
        project,
        [
            f"CREATE TABLE IF NOT EXISTS {{db}}.owned ON CLUSTER {cluster.name} (id UInt64) ENGINE=Memory",
            f"ALTER TABLE {{db}}.owned ON CLUSTER {cluster.name} ADD COLUMN IF NOT EXISTS value String",
        ],
    )
    result = project.run("up", "it")
    assert result.exit_code == 0, result.output
    for client in cluster.clients.values():
        assert client.query(
            "SELECT name FROM system.columns WHERE database={db:String} AND table='owned' ORDER BY position",
            parameters={"db": project.database},
        ).result_rows == [("id",), ("value",)]
    rows = _queue(project)
    assert len({row[0] for row in rows}) == 2
    assert all(row[3] == "Finished" and row[4] == 0 for row in rows)
    assert _heads(project) == ["bbbb"]


def test_ddl_queue_down_host_times_out_then_reattaches(cluster_project, clickhouse_cluster):
    project, cluster = cluster_project, clickhouse_cluster
    _baseline(project)
    _revision(
        project,
        [
            f"CREATE TABLE IF NOT EXISTS {{db}}.owned ON CLUSTER {cluster.name} (id UInt64) ENGINE=Memory"
        ],
    )
    cluster.stop_node(2)
    try:
        result = project.run("up", "it", "--timeout", "1")
        assert result.exit_code == 1 and "Timed out" in result.output, result.output
        assert cluster.hostnames[2] in result.output
        before = {row[0] for row in _queue(project)}
        assert len(before) == 1
        assert _heads(project) == ["aaaa"]
    finally:
        cluster.start_node(2)
    resumed = project.run("up", "it")
    assert resumed.exit_code == 0, resumed.output
    assert {row[0] for row in _queue(project)} == before
    assert all(row[3] == "Finished" and row[4] == 0 for row in _queue(project))
    assert _heads(project) == ["bbbb"]


def test_ddl_queue_host_exception_is_not_finished_success(cluster_project, clickhouse_cluster):
    project, cluster = cluster_project, clickhouse_cluster
    _baseline(project)
    table = f"{project.database}.owned"
    project.client.command(
        f"CREATE TABLE {table} ON CLUSTER {cluster.name} (id UInt64) ENGINE=Memory"
    )
    cluster.clients[2].command(f"ALTER TABLE {table} ADD COLUMN conflict String")
    _revision(
        project, [f"ALTER TABLE {{db}}.owned ON CLUSTER {cluster.name} ADD COLUMN conflict UInt64"]
    )
    result = project.run("up", "it")
    assert result.exit_code == 1, result.output
    assert cluster.hostnames[2] in result.output and "DUPLICATE_COLUMN" in result.output
    assert _heads(project) == ["aaaa"]
    assert any(row[3] == "Finished" and row[4] == 15 for row in _queue(project))


def test_ddl_queue_lost_acknowledgement_uses_pre_send_marker(cluster_project, clickhouse_cluster):
    project, cluster = cluster_project, clickhouse_cluster
    _baseline(project)
    _revision(
        project,
        [
            f"CREATE TABLE IF NOT EXISTS {{db}}.owned ON CLUSTER {cluster.name} (id UInt64) ENGINE=Memory"
        ],
    )
    code = """import os, signal
from ch_migrate.waiting import MigrationWaiter
original = MigrationWaiter._after
def crash(self, connection, cursor, statement, parameters, context, executemany):
    pending = self._pending.get(id(context))
    if pending and pending[1].get('ddl') and pending[1].get('table', [])[-1] == 'owned':
        os.kill(os.getpid(), signal.SIGKILL)
    return original(self, connection, cursor, statement, parameters, context, executemany)
MigrationWaiter._after = crash
from ch_migrate.cli import main
main()
"""
    with _running(project, code) as (process, log):
        assert process.wait(timeout=30) == -9, log.read_text()
    entries = {row[0] for row in _queue(project)}
    assert len(entries) == 1
    resumed = project.run("up", "it")
    assert resumed.exit_code == 0, resumed.output
    assert {row[0] for row in _queue(project)} == entries
    assert _heads(project) == ["bbbb"]


def test_ddl_queue_and_replicated_mutation_both_must_finish(cluster_project, clickhouse_cluster):
    project, cluster = cluster_project, clickhouse_cluster
    _baseline(project)
    table = f"{project.database}.owned"
    project.client.command(
        f"CREATE TABLE {table} ON CLUSTER {cluster.name} (id UInt64, x UInt64) "
        f"ENGINE=ReplicatedMergeTree('/clickhouse/ddl_wait/{project.database}', '{{replica}}') ORDER BY id"
    )
    project.client.command(f"INSERT INTO {table} VALUES (1, 0)")
    cluster.clients[2].command(f"SYSTEM SYNC REPLICA {table}")
    _revision(
        project, [f"ALTER TABLE {{db}}.owned ON CLUSTER {cluster.name} UPDATE x=x+1 WHERE id=1"]
    )
    for client in cluster.clients.values():
        client.command(f"SYSTEM STOP MERGES {table}")
    try:
        result = project.run("up", "it", "--timeout", "1")
        assert result.exit_code == 1, result.output
        rows = _queue(project)
        assert rows and all(row[3] == "Finished" and row[4] == 0 for row in rows)
        assert "parts_to_do=" in result.output
        assert _heads(project) == ["aaaa"]
        entries = {row[0] for row in rows}
    finally:
        for client in cluster.clients.values():
            client.command(f"SYSTEM START MERGES {table}")
    resumed = project.run("up", "it")
    assert resumed.exit_code == 0, resumed.output
    assert {row[0] for row in _queue(project)} == entries
    for client in cluster.clients.values():
        assert client.command(f"SELECT x FROM {table}") == 1
    assert _heads(project) == ["bbbb"]


def test_ddl_queue_missing_ownership_evidence_refuses_resubmission(
    cluster_project, clickhouse_cluster
):
    project, cluster = cluster_project, clickhouse_cluster
    _baseline(project)
    _revision(
        project,
        [
            f"CREATE TABLE IF NOT EXISTS {{db}}.owned ON CLUSTER {cluster.name} (id UInt64) ENGINE=Memory"
        ],
    )
    cluster.stop_node(2)
    try:
        assert project.run("up", "it", "--timeout", "1").exit_code == 1
        entries = {row[0] for row in _queue(project)}
        row = project.client.query(
            f"SELECT generation, position, sequence, payload FROM {project.database}._ch_migrate_journal "
            "WHERE revision='bbbb' ORDER BY sequence DESC LIMIT 1"
        ).result_rows[0]
        payload = json.loads(row[3])
        assert "ddl" in payload
        # An operator-restored receipt whose ownership token has no server evidence.
        payload["ddl"]["token"] = "missing-ownership-evidence"
        project.client.insert(
            f"{project.database}._ch_migrate_journal",
            [["bbbb", row[0], row[1], row[2] + 1, json.dumps(payload)]],
            column_names=["revision", "generation", "position", "sequence", "payload"],
        )
        result = project.run("up", "it")
        assert result.exit_code == 1 and "unknown" in result.output.lower(), result.output
        assert "not reissued" in result.output
        assert {row[0] for row in _queue(project)} == entries
        assert _heads(project) == ["aaaa"]
    finally:
        cluster.start_node(2)


def test_ddl_queue_is_not_used_for_single_node_migrations(project):
    project.write_revision(
        "aaaa",
        {
            "upgrade": 'op.execute(f"CREATE TABLE IF NOT EXISTS {db}.owned (id UInt64) ENGINE=Memory")'
        },
    )
    result = project.run("up", "it")
    assert result.exit_code == 0, result.output
    project.client.command("SYSTEM FLUSH LOGS")
    reads = project.client.query(
        "SELECT count() FROM system.query_log WHERE type='QueryStart' "
        "AND position(query, 'FROM system.distributed_ddl_queue') > 0 "
        "AND position(query, 'system.query_log') = 0"
    ).result_rows[0][0]
    assert reads == 0


def test_ddl_queue_ttl_metadata_and_materialization_resume(cluster_project, clickhouse_cluster):
    project, cluster = cluster_project, clickhouse_cluster
    _baseline(project)
    table = f"{project.database}.expiry"
    project.client.command(
        f"CREATE TABLE {table} ON CLUSTER {cluster.name} (ts DateTime) "
        f"ENGINE=ReplicatedMergeTree('/clickhouse/ddl_ttl/{project.database}', '{{replica}}') ORDER BY ts"
    )
    project.client.command(f"INSERT INTO {table} VALUES ('2020-01-01 00:00:00')")
    cluster.clients[2].command(f"SYSTEM SYNC REPLICA {table}")
    _revision(
        project,
        [f"ALTER TABLE {{db}}.expiry ON CLUSTER {cluster.name} MODIFY TTL ts + INTERVAL 100 YEAR"],
    )
    for client in cluster.clients.values():
        client.command(f"SYSTEM STOP MERGES {table}")
    try:
        result = project.run("up", "it", "--timeout", "1")
        assert result.exit_code == 1 and "parts_to_do=" in result.output, result.output
        entries = {row[0] for row in _queue(project)}
        assert len(entries) == 2  # The metadata and materialization have separate receipts.
        assert _heads(project) == ["aaaa"]
    finally:
        for client in cluster.clients.values():
            client.command(f"SYSTEM START MERGES {table}")
    resumed = project.run("up", "it")
    assert resumed.exit_code == 0, resumed.output
    assert {row[0] for row in _queue(project)} == entries
    assert _heads(project) == ["bbbb"]


def test_ddl_queue_nonreplicated_mutations_wait_on_each_host(cluster_project, clickhouse_cluster):
    project, cluster = cluster_project, clickhouse_cluster
    _baseline(project)
    table = f"{project.database}.owned"
    project.client.command(
        f"CREATE TABLE {table} ON CLUSTER {cluster.name} (x UInt64) ENGINE=MergeTree ORDER BY tuple()"
    )
    for client in cluster.clients.values():
        client.command(f"INSERT INTO {table} VALUES (0)")
    _revision(project, [f"ALTER TABLE {{db}}.owned ON CLUSTER {cluster.name} UPDATE x=x+1 WHERE 1"])
    cluster.clients[2].command(f"SYSTEM STOP MERGES {table}")
    try:
        result = project.run("up", "it", "--timeout", "1")
        assert result.exit_code == 1, result.output
        assert cluster.hostnames[2] in result.output
        assert _heads(project) == ["aaaa"]
        assert cluster.clients[1].command(f"SELECT x FROM {table}") == 1
        assert cluster.clients[2].command(f"SELECT x FROM {table}") == 0
    finally:
        cluster.clients[2].command(f"SYSTEM START MERGES {table}")
    resumed = project.run("up", "it")
    assert resumed.exit_code == 0, resumed.output
    for client in cluster.clients.values():
        assert client.command(f"SELECT x FROM {table}") == 1


def test_ddl_queue_cold_version_table_creation_obeys_timeout(cluster_project, clickhouse_cluster):
    project, cluster = cluster_project, clickhouse_cluster
    project.write_revision("aaaa", {"upgrade": 'op.execute("SELECT 1")'})
    cluster.stop_node(2)
    try:
        result = project.run("up", "it", "--timeout", "1")
        assert result.exit_code == 1 and "Timed out" in result.output, result.output
        assert cluster.hostnames[2] in result.output
        assert _heads(project) == []
    finally:
        cluster.start_node(2)
    resumed = project.run("up", "it")
    assert resumed.exit_code == 0, resumed.output
    assert _heads(project) == ["aaaa"]


def _baseline(project):
    project.write_revision("aaaa", {"upgrade": 'op.execute("SELECT 1")'})
    result = project.run("up", "it")
    assert result.exit_code == 0, result.output


def _revision(project, statements):
    path = project.sql_dir / "ddl.sql"
    path.write_text(
        "\n".join(
            "-- ch-migrate: allow-non-idempotent Controlled distributed DDL scenario\n" + sql + ";"
            for sql in statements
        )
    )
    project.write_revision(
        "bbbb", {"upgrade": "from ch_migrate import run_sql\nrun_sql('ddl.sql')"}, "aaaa"
    )


def _queue(project):
    return project.client.query(
        "SELECT entry, host, port, status, exception_code FROM system.distributed_ddl_queue "
        "WHERE startsWith(settings['log_comment'], 'chm_mutation_') AND position(query, {db:String}) > 0 "
        "AND position(query, 'alembic_version') = 0 AND position(query, '_ch_migrate_journal') = 0",
        parameters={"db": project.database},
    ).result_rows


def _heads(project):
    return [
        row[0]
        for row in project.client.query(
            f"SELECT version_num FROM {project.database}.alembic_version ORDER BY version_num"
        ).result_rows
    ]


@contextmanager
def _running(project, code):
    path = project.root / f"ddl-{time.monotonic_ns()}.log"
    with path.open("w") as log:
        process = subprocess.Popen(
            [sys.executable, "-c", code, "up", "it"],
            cwd=project.root,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=os.environ.copy(),
        )
        try:
            yield process, path
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
