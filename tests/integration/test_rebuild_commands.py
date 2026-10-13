"""Rebuild acceptance on the fixtures' owned servers and, where portable, ClickHouse Cloud."""

from __future__ import annotations

import http.client
import json
import os
import secrets
import signal
import ssl
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import certifi
import pytest
import yaml

pytestmark = pytest.mark.integration


@pytest.fixture
def owned_project(request):
    if os.environ.get("CH_MIGRATE_IT_URL"):
        pytest.skip("Rebuild acceptance requires the fixture-owned Docker server")
    return request.getfixturevalue("project")


def test_rebuild_sorting_key_preserves_rows_and_rollback(project):
    before = _seed(project, 12000)
    original_uuid = _uuid(project)
    _revision(project)
    applied = project.run("up", "it")
    assert applied.exit_code == 0, applied.output
    assert "ORDER BY (k, ts, id)" in project.client.command(
        f"SHOW CREATE TABLE {project.database}.events"
    )
    assert _checksum(project) == before
    helpers = _helpers(project)
    assert len(helpers) == 1, helpers
    result = _rebuild_result(project)
    assert result["rollback_table"] == helpers[0]
    assert _checksum(project, helpers[0]) == before
    assert original_uuid in result["old_uuids"].values()
    assert _uuid(project) in result["new_uuids"].values()
    assert _uuid(project) != original_uuid
    assert result["duplicate_window"]["start"] <= result["duplicate_window"]["end"]
    assert set(result["async_flush_errors"]) == {
        "count",
        "bytes",
        "query_ids",
        "complete",
        "replicas",
    }
    assert result["async_flush_errors"]["complete"] is not project.cloud
    assert _heads(project) == ["bbbb"]
    rerun = project.run("up", "it")
    assert rerun.exit_code == 0, rerun.output
    assert _checksum(project) == before
    assert _helpers(project) == helpers
    project.client.command(f"DROP TABLE {project.database}.{result['rollback_table']} SYNC")
    assert _helpers(project) == []
    assert _checksum(project) == before


def test_rebuild_sigkill_requires_operator_release_then_resumes(project):
    before = _seed(project, 120000, first_partition_rows=200)
    source_uuid = _uuid(project)
    _revision(project, select=_slow_projection())
    first_partition = "202601"
    with _running(project) as (first, log):
        orphan = _until(
            lambda: _copy_after_moved_partition(project, first_partition),
            "First partition never moved before the second partition copy",
            seconds=90,
        )
        owner = _lock_owner(project)
        first.kill()
        assert first.wait(timeout=15) != 0
        assert first.poll() is not None
        assert _until(lambda: _active_copy(project) == orphan, "Server-side orphan disappeared")
        _assert_locked_refusal(project, owner, orphan)
        _until(lambda: _lock_age(project) >= 9, "Killed owner's heartbeat did not expire", 30)
        _assert_locked_refusal(project, owner, orphan)
    assert _heads(project) == ["aaaa"]
    assert _uuid(project) == source_uuid
    _operator_release(project, first, owner)
    resumed = project.run("up", "it", "--timeout", "180")
    assert resumed.exit_code == 0, resumed.output + log.read_text()
    assert not _active_copy(project)
    assert not _locks(project)
    assert _checksum(project) == before
    assert _partition_rows(project, "events", first_partition) == 200
    project.flush_logs()
    completed = project.client.command(
        f"SELECT count() FROM {project.query_log()} WHERE type = 'QueryFinish' AND is_initial_query "
        "AND startsWith(query_id, 'chm-rebuild-') AND endsWith(query_id, {part:String}) "
        "AND position(query, {db:String}) > 0",
        parameters={"part": first_partition, "db": project.database},
    )
    assert completed == 1, "Resume must not recopy an already moved partition"


def test_rebuild_stale_live_owner_cannot_be_supplanted_before_exchange(project):
    before = _seed(project, 120000)
    original_uuid = _uuid(project)
    _revision(project, select=_slow_projection())
    with _running(project) as (first, log):
        orphan = _until(lambda: _active_copy(project), "Owner never started copying")
        owner = _lock_owner(project)
        os.kill(first.pid, signal.SIGSTOP)
        try:
            _until(lambda: _lock_age(project) >= 9, "Paused owner's heartbeat did not expire", 30)
            _assert_locked_refusal(project, owner, orphan)
            assert _uuid(project) == original_uuid, "Competitor swapped while owner was paused"
            assert first.poll() is None
        finally:
            os.kill(first.pid, signal.SIGCONT)
        assert first.wait(timeout=180) == 0, log.read_text()
    assert _checksum(project) == before
    assert _uuid(project) != original_uuid
    assert not _locks(project)


def test_rebuild_concurrent_up_refuses_second_owner(project):
    before = _seed(project, 80000)
    _revision(project, select=_slow_projection())
    with _running(project) as (first, log):
        orphan = _until(lambda: _active_copy(project), "First runner never started copying")
        owner = _lock_owner(project)
        _assert_locked_refusal(project, owner, orphan)
        assert first.poll() is None
        assert first.wait(timeout=180) == 0, log.read_text()
    assert _checksum(project) == before
    assert _heads(project) == ["bbbb"]
    assert len(_helpers(project)) == 1


@pytest.mark.parametrize("case", ["distributed", "partition", "mutation", "engine", "async0"])
def test_rebuild_preflight_refuses_without_helpers(project, case):
    _seed(project, 10000)
    target = _target(project)
    if case == "distributed":
        cluster = project.client.command("SELECT any(cluster) FROM system.clusters")
        assert cluster, "Owned ClickHouse fixture has no configured cluster for a Distributed table"
        project.client.command(
            f"RENAME TABLE {project.database}.events TO {project.database}.local_events"
        )
        project.client.command(
            f"CREATE TABLE {project.database}.events (id UInt64, ts DateTime, k UInt8, payload String) "
            f"ENGINE = Distributed('{cluster}', '{project.database}', 'local_events', id)"
        )
    elif case == "partition":
        target = target.replace("PARTITION BY toYYYYMM(ts)", "PARTITION BY toYYYYMM(ts) + 1")
    elif case == "mutation":
        project.stop_merges(f"{project.database}.events")
        project.client.command(
            f"ALTER TABLE {project.database}.events UPDATE k = k + 1 WHERE id = 1"
        )
        _until(lambda: _pending_mutation(project), "Mutation did not become pending")
    elif case == "engine":
        target = target.replace("ENGINE = MergeTree()", "ENGINE = Memory")
    else:
        _send_async0(project, "preflight-" + secrets.token_hex(6), 900001)
        _until(lambda: _queued_async(project), "Async wait0 insert was not queued")
    try:
        _revision(project, target=target)
        rejected = project.run("up", "it", "--timeout", "5")
        assert rejected.exit_code != 0, rejected.output
        assert _heads(project) == ["aaaa"]
        assert not _helpers(project)
        assert not _locks(project)
    finally:
        if case == "mutation":
            project.client.command(f"SYSTEM START MERGES {project.database}.events")
        if case == "async0":
            project.drain_async_queue("events")


def test_rebuild_replicated_database_updates_both_replicas(
    cluster_project, clickhouse_cluster, request
):
    cluster = clickhouse_cluster
    database = "it_rep_" + secrets.token_hex(8)
    for node, client in cluster.clients.items():
        request.addfinalizer(
            lambda owned=client: owned.command(f"DROP DATABASE IF EXISTS {database} SYNC")
        )
        client.command(
            f"CREATE DATABASE {database} ENGINE = Replicated('/clickhouse/databases/{database}', '01', 'r{node}') "
            "SETTINGS collection_name = 'chm_it_auth'"
        )
    config_path = cluster_project.root / "config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["environments"]["it"]["database"] = database
    config["environments"]["it"].pop("cluster")
    config_path.write_text(yaml.safe_dump(config))
    project = replace(cluster_project, database=database)
    project.client.command(
        f"CREATE TABLE {database}.events (id UInt64, ts DateTime, k UInt8, payload String) "
        "ENGINE = ReplicatedMergeTree PARTITION BY toYYYYMM(ts) ORDER BY id"
    )
    _baseline(project)
    project.client.command(
        f"INSERT INTO {database}.events SELECT number, toDateTime('2026-01-01') + number, "
        "number % 7, toString(number) FROM numbers(4000)"
    )
    cluster.clients[2].command(f"SYSTEM SYNC REPLICA {database}.events")
    before = _checksum(project)
    _revision(project, target=_target(project, engine="ReplicatedMergeTree"))
    for client in cluster.clients.values():
        client.command("SYSTEM FLUSH LOGS")
    result = project.run("up", "it", "--timeout", "180")
    assert result.exit_code == 0, result.output
    for client in cluster.clients.values():
        client.command(f"SYSTEM SYNC REPLICA {database}.events")
        assert client.command(f"SELECT count() FROM {database}.events") == before[0]
        assert (
            client.command(f"SELECT sum(cityHash64(id, ts, k, payload)) FROM {database}.events")
            == before[1]
        )
        assert "ORDER BY (k, ts, id)" in client.command(f"SHOW CREATE TABLE {database}.events")
        assert client.query(
            "SELECT total_replicas FROM system.replicas WHERE database = {db:String} AND table = 'events'",
            parameters={"db": database},
        ).result_rows == [(2,)]
    report = _rebuild_result(project)
    assert len(report["old_uuids"]) == len(report["new_uuids"]) == 2


@pytest.mark.parametrize("shared_source_uuid", [True, False])
def test_rebuild_configured_atomic_cluster_waits_for_both_replicas(
    cluster_project, clickhouse_cluster, shared_source_uuid
):
    project, cluster = cluster_project, clickhouse_cluster
    table = f"{project.database}.events"
    schema = (
        "(id UInt64, ts DateTime, k UInt8, payload String) "
        f"ENGINE = ReplicatedMergeTree('/chm-rebuild-test/{project.database}/events', '{{replica}}') "
        "PARTITION BY toYYYYMM(ts) ORDER BY id"
    )
    if shared_source_uuid:
        project.client.command(f"CREATE TABLE {table} ON CLUSTER {cluster.name} {schema}")
    else:
        for client in cluster.clients.values():
            client.command(f"CREATE TABLE {table} UUID '{uuid4()}' {schema}")
    _baseline(project)
    project.client.command(
        f"INSERT INTO {table} SELECT number, toDateTime('2026-01-01'), number % 7, toString(number) "
        "FROM numbers(3000)"
    )
    for client in cluster.clients.values():
        client.command(f"SYSTEM SYNC REPLICA {table}")
        client.command("SYSTEM FLUSH LOGS")
    before = _checksum(project)
    explicit_engine = (
        f"ReplicatedMergeTree('/chm-rebuild-test/{project.database}/events', '{{replica}}')"
    )
    _revision(project, target=_target(project, engine=explicit_engine))
    if shared_source_uuid:
        config_path = project.root / "config.yaml"
        original_config = config_path.read_text()
        unscoped = yaml.safe_load(original_config)
        unscoped["environments"]["it"].pop("cluster")
        config_path.write_text(yaml.safe_dump(unscoped))
        refused = project.run("up", "it", "--timeout", "10")
        assert refused.exit_code != 0 and "incomplete_replica_scope" in refused.output
        assert not _locks(project) and not _helpers(project)
        config_path.write_text(original_config)
    applied = project.run("up", "it", "--timeout", "90")
    assert applied.exit_code == 0, applied.output
    for client in cluster.clients.values():
        assert (
            client.query(
                f"SELECT count(), sum(cityHash64(id, ts, k, payload)) FROM {table}"
            ).result_rows[0]
            == before
        )
        assert "ORDER BY (k, ts, id)" in client.command(f"SHOW CREATE TABLE {table}")
    assert len(_rebuild_result(project)["new_uuids"]) == 2
    assert len(set(_rebuild_result(project)["old_uuids"].values())) == (
        1 if shared_source_uuid else 2
    )
    rollback = _rebuild_result(project)["rollback_table"]
    project.client.command(
        f"DROP TABLE {project.database}.{rollback} ON CLUSTER {cluster.name} SYNC"
    )
    project.client.command(f"INSERT INTO {table} VALUES (4001, '2026-01-02', 2, 'after cleanup')")
    for client in cluster.clients.values():
        client.command(f"SYSTEM SYNC REPLICA {table}")
        assert client.command(f"SELECT count() FROM {table}") == before[0] + 1


def test_rebuild_after_exchange_resumes_cleanup_only(project, clickhouse_server):
    before = _seed(project, 2000)
    old_uuid = _uuid(project)
    _revision(project)
    prefix = f"DROP TABLE `{project.database}`.`events__chm_dual`"
    with _hold_command(project, clickhouse_server, prefix) as (blocked, release):
        with _running(project) as (process, log):
            assert blocked.wait(30), log.read_text()
            rebuilt_uuid = _uuid(project)
            assert rebuilt_uuid != old_uuid
            owner = _lock_owner(project)
            process.kill()
            assert process.wait(timeout=15) != 0
            release.set()
            _until(
                lambda: project.client.command(
                    "SELECT count() FROM system.tables WHERE database={db:String} AND name='events__chm_dual'",
                    parameters={"db": project.database},
                )
                == 0,
                "Owned DROP did not complete",
            )
    assert process.poll() is not None
    assert _lock_owner(project) == owner
    assert _heads(project) == ["aaaa"]
    project.client.command(f"DROP TABLE {project.database}._chm_rebuild_lock_events SYNC")
    resumed = project.run("up", "it", "--timeout", "30")
    assert resumed.exit_code == 0, resumed.output + log.read_text()
    assert _uuid(project) == rebuilt_uuid
    assert _checksum(project) == before
    assert _helpers(project) == [_rebuild_result(project)["rollback_table"]]
    assert _heads(project) == ["bbbb"]


def test_rows_stranded_in_old_storage_stop_for_the_operator(project, clickhouse_server):
    """A write that reaches only the old storage after the swap stops the run until released."""
    before = _seed(project, 2000)
    _revision(project)
    db, rollback = project.database, "events__chm_old_bbbb"
    insert = f"INSERT INTO {db}.{rollback} VALUES (9000000, '2026-02-02', 1, 'stranded')"
    with _hold_command(project, clickhouse_server, "SELECT query_id, sum(rows) FROM") as gate:
        blocked, release = gate
        with _running(project) as (process, log):
            # Cloud reaches the check only after its async-log reads settle (40 s seen).
            assert blocked.wait(180), log.read_text()
            # Stands in for an insert that resolved the old table just before EXCHANGE.
            project.client.command(insert)
            project.flush_logs()
            release.set()
            assert process.wait(timeout=90) != 0
    assert "reached only" in log.read_text(), log.read_text()
    assert _heads(project) == ["aaaa"] and _locks(project)
    refused = project.run("up", "it", "--timeout", project.budget(5))
    assert refused.exit_code != 0, refused.output
    project.client.command(
        f"INSERT INTO {db}.events SELECT * FROM {db}.{rollback} WHERE id = 9000000"
    )
    project.client.command(f"DROP TABLE {db}._chm_rebuild_lock_events SYNC")
    resumed = project.run("up", "it", "--timeout", project.budget(30))
    assert resumed.exit_code == 0, resumed.output
    stranded = _rebuild_result(project)["stranded"]
    assert stranded["rows"] == 1 and stranded["acknowledged"]
    assert stranded["query_ids"] == [
        project.client.command(
            # part_log names the flush query when the server buffers the insert asynchronously.
            f"SELECT any(query_id) FROM {project.query_log()} WHERE type = 'QueryFinish' "
            "AND query_kind IN ('Insert', 'AsyncInsertFlush') "
            "AND position(query, {table:String}) > 0",
            parameters={"table": f"{db}.{rollback}"},
        )
    ]
    assert _checksum(project)[0] == before[0] + 1
    assert (
        project.client.command(f"SELECT payload FROM {db}.events WHERE id = 9000000") == "stranded"
    )
    assert _heads(project) == ["bbbb"]


def test_rebuild_async0_opt_in_reports_old_uuid_flush_errors(project, clickhouse_server):
    before = _seed(project, 20000)
    old_uuid = _uuid(project)
    _revision(project, allow_async_loss=True)
    query_id = "async0-" + secrets.token_hex(8)
    # Hold the real HTTP EXCHANGE request (after the pre-swap flush where one is allowed).
    # Enqueue exactly one old-UUID insert in that gap, then let ClickHouse swap.
    with _hold_command(project, clickhouse_server, "EXCHANGE TABLES") as (blocked, release):
        with _running(project) as (process, log):
            assert blocked.wait(30), log.read_text()
            try:
                _send_async0(project, query_id, 1000000)
                assert _queued_async(project)
            finally:
                release.set()
            assert process.wait(timeout=90) == 0, log.read_text()
    report = _rebuild_result(project)["async_flush_errors"]
    project.flush_logs()
    errors = project.client.query(
        f"SELECT query_id, bytes FROM {project.everywhere('system.asynchronous_insert_log')} "
        "WHERE database = {db:String} AND table = 'events' AND status = 'FlushError' "
        "AND position(exception, {uuid:String}) > 0",
        parameters={"db": project.database, "uuid": old_uuid},
    ).result_rows
    assert len(errors) == 1 and errors[0][0] == query_id
    assert (report["count"], report["bytes"], report["query_ids"]) == (1, errors[0][1], [query_id])
    assert report["complete"] is not project.cloud
    assert _checksum(project) == before
    assert _heads(project) == ["bbbb"]


def test_plan_understands_guarded_rebuild_without_running_it(project):
    before = _seed(project, 1000)
    source_uuid = _uuid(project)
    _revision(project)
    planned = project.run("plan", "it", "--json")
    assert planned.exit_code == 0, planned.output
    document = json.loads(planned.stdout)
    assert document["gate_would_refuse"] is False
    statement = document["migrations"][0]["statements"][0]
    assert statement["classification"]["kind"] == "rebuild"
    assert statement["file"] == "migrations/sql/rebuild.sql"
    assert statement["rebuild"]["part_count"] == 1
    assert _uuid(project) == source_uuid
    assert _checksum(project) == before
    assert not _helpers(project) and not _locks(project)


@pytest.mark.parametrize("masked", [False, True])
def test_rebuild_preserves_empty_logical_tables(project, masked):
    _seed(project, 30 if masked else 0)
    if masked:
        project.client.command(
            f"DELETE FROM {project.database}.events WHERE 1 SETTINGS lightweight_deletes_sync=1"
        )
    assert _checksum(project)[0] == 0
    source_uuid = _uuid(project)
    _revision(project)
    applied = project.run("up", "it")
    assert applied.exit_code == 0, applied.output
    assert _uuid(project) != source_uuid
    assert _checksum(project)[0] == 0
    assert "ORDER BY (k, ts, id)" in project.client.command(
        f"SHOW CREATE TABLE {project.database}.events"
    )
    assert _heads(project) == ["bbbb"]


def test_completed_rebuild_is_not_repeated_after_later_interruption(project):
    before = _seed(project, 2000)
    _revision(project)
    revision = project.versions_dir / "bbbb.py"
    revision.write_text(
        revision.read_text().replace(
            "def downgrade():", "    op.execute('SELECT sleep(2)')\n\ndef downgrade():"
        )
    )
    with _running(project) as (process, log):
        _until(
            lambda: project.client.command(
                f"SELECT count() FROM {project.everywhere('system.processes')} "
                "WHERE startsWith(query, 'SELECT sleep(2)')"
            ),
            "Rebuild did not reach its following read",
            seconds=120,
        )
        assert not _locks(project)
        rebuilt_uuid = _uuid(project)
        process.kill()
        assert process.wait(timeout=15) != 0
    assert _heads(project) == ["aaaa"]
    resumed = project.run("up", "it")
    assert resumed.exit_code == 0, resumed.output + log.read_text()
    assert _uuid(project) == rebuilt_uuid
    assert _checksum(project) == before
    assert len(_helpers(project)) == 1
    assert _heads(project) == ["bbbb"]


def test_rebuild_survives_helper_merges_resuming_mid_copy(owned_project):
    """A server restart re-enables merges; snapshot helpers must still keep every counted row."""
    project = owned_project
    db = project.database
    project.client.command(
        f"CREATE TABLE {db}.events (id UInt64, ts DateTime, k UInt8, payload String) "
        "ENGINE = ReplacingMergeTree PARTITION BY toYYYYMM(ts) ORDER BY id"
    )
    _baseline(project)
    project.client.command(f"SYSTEM STOP MERGES {db}.events")  # Keep duplicate rows to snapshot.
    for _ in range(2):
        project.client.command(
            f"INSERT INTO {db}.events SELECT number, toDateTime('2026-02-01') + number, "
            "number % 7, toString(number) FROM numbers(60000)"
        )
    project.client.command("SYSTEM FLUSH LOGS")
    before = project.client.query(
        f"SELECT count(), sum(cityHash64(id, ts, k, payload)) FROM {db}.events FINAL"
    ).result_rows[0]
    # Slow enough to act mid-copy, within ClickHouse's 3 s sleep-per-block limit.
    slow = "id + toUInt64(sleepEachRow(0.0001)) AS id, ts, k, payload"
    _revision(project, target=_target(project, "ReplacingMergeTree"), select=slow)
    try:
        with _running(project, timeout="90") as (process, log):
            _until(lambda: _active_copy(project), "Copy never started", seconds=60)
            for role in ("snap", "stage"):
                helper = f"{db}.events__chm_{role}"
                project.client.command(f"SYSTEM START MERGES {helper}")
                project.client.command(f"OPTIMIZE TABLE {helper} FINAL")
            assert process.wait(timeout=120) == 0, log.read_text()
    finally:
        project.client.command(f"SYSTEM START MERGES {db}.events")
    after = project.client.query(
        f"SELECT count(), sum(cityHash64(id, ts, k, payload)) FROM {db}.events FINAL"
    ).result_rows[0]
    assert after == before
    assert _heads(project) == ["bbbb"]


def test_rebuild_survives_replica_restart_mid_copy(cluster_project, clickhouse_cluster):
    """A real restart of the other replica mid-copy: merges resume there, and nothing is lost."""
    project, cluster = cluster_project, clickhouse_cluster
    table = f"{project.database}.events"
    engine = (
        f"ReplicatedReplacingMergeTree('/chm-restart/{project.database}/events', '{{replica}}')"
    )
    project.client.command(
        f"CREATE TABLE {table} ON CLUSTER {cluster.name} "
        f"(id UInt64, ts DateTime, k UInt8, payload String) ENGINE = {engine} "
        "PARTITION BY toYYYYMM(ts) ORDER BY id"
    )
    _baseline(project)
    for client in cluster.clients.values():
        client.command(f"SYSTEM STOP MERGES {table}")  # Keep duplicate rows to snapshot.
    for _ in range(2):
        project.client.command(
            f"INSERT INTO {table} SELECT number, toDateTime('2026-02-01') + number, "
            "number % 7, toString(number) FROM numbers(60000)"
        )
    for client in cluster.clients.values():
        client.command(f"SYSTEM SYNC REPLICA {table}")
        client.command("SYSTEM FLUSH LOGS")
    before = _final_checksum(project.client, table)
    target = _target(
        project, f"ReplicatedReplacingMergeTree('/chm-restart/{{db}}/new', '{{replica}}')"
    )
    slow = "id + toUInt64(sleepEachRow(0.0001)) AS id, ts, k, payload"
    _revision(project, target=target, select=slow)
    try:
        _restart_replica_mid_copy(project, cluster)
    finally:
        for client in cluster.clients.values():
            client.command(f"SYSTEM START MERGES {table}")
    assert _heads(project) == ["bbbb"]
    for client in cluster.clients.values():
        client.command(f"SYSTEM SYNC REPLICA {table}")
        assert _final_checksum(client, table) == before


def _restart_replica_mid_copy(project, cluster):
    with _running(project, timeout="180") as (process, log):
        _until(lambda: _active_copy(project), "Copy never started", seconds=60)
        cluster.stop_node(2)
        cluster.start_node(2)  # Node 2's STOP MERGES on the helpers, if any, is now gone.
        exit_code = process.wait(timeout=180)
    if exit_code != 0:  # A rebuild may stop while node 2 is down; the next `up` resumes it.
        resumed = project.run("up", "it", "--timeout", "180")
        assert resumed.exit_code == 0, log.read_text() + resumed.output


def _final_checksum(client, table):
    return client.query(
        f"SELECT count(), sum(cityHash64(id, ts, k, payload)) FROM {table} FINAL"
    ).result_rows[0]


def _baseline(project):
    project.write_revision("aaaa", {"upgrade": 'op.execute("SELECT 1")'})
    result = project.run("up", "it")
    assert result.exit_code == 0, result.output


def _seed(project, rows, first_partition_rows=0):
    db = project.database
    project.client.command(
        f"CREATE TABLE {db}.events (id UInt64, ts DateTime, k UInt8, payload String) "
        "ENGINE = MergeTree() PARTITION BY toYYYYMM(ts) ORDER BY id"
    )
    _baseline(project)
    if first_partition_rows:
        project.client.command(
            f"INSERT INTO {db}.events SELECT number, toDateTime('2026-01-01') + number, "
            f"number % 7, toString(number) FROM numbers({first_partition_rows})"
        )
    project.client.command(
        f"INSERT INTO {db}.events SELECT 100000 + number, toDateTime('2026-02-01') + number, "
        f"number % 7, toString(number) FROM numbers({rows})"
    )
    project.flush_logs()  # Observer seeds complete writer evidence.
    return _checksum(project)


def _target(project, engine="MergeTree()"):
    return (
        "CREATE TABLE {db}.events (id UInt64, ts DateTime, k UInt8, payload String) "
        f"ENGINE = {engine} PARTITION BY toYYYYMM(ts) ORDER BY (k, ts, id)"
    )


def _revision(project, *, target=None, select=None, allow_async_loss=False):
    (project.sql_dir / "rebuild.sql").write_text(target or _target(project))
    options = []
    if select is not None:
        options.append(f"select={select!r}")
    if allow_async_loss:
        options.append("allow_unacknowledged_async_loss=True")
    if project.cloud:
        options.append("writers_on_this_service_only=True")  # The test's writers use one service.
    suffix = ", " + ", ".join(options) if options else ""
    project.write_revision(
        "bbbb", {"upgrade": f"op.rebuild_table('events', 'rebuild.sql'{suffix})"}, "aaaa"
    )


def _slow_projection():
    return "id + toUInt64(sleepEachRow(0.0003)) AS id, ts, k, payload"


def _checksum(project, name="events"):
    return project.client.query(
        f"SELECT count(), sum(cityHash64(id, ts, k, payload)) FROM {project.database}.{name}"
    ).result_rows[0]


def _partition_rows(project, name, partition):
    return project.client.command(
        f"SELECT count() FROM {project.database}.{name} WHERE _partition_id = {{part:String}}",
        parameters={"part": partition},
    )


def _heads(project):
    return [
        row[0]
        for row in project.client.query(
            f"SELECT version_num FROM {project.database}.alembic_version ORDER BY version_num"
        ).result_rows
    ]


def _helpers(project):
    return [
        row[0]
        for row in project.client.query(
            "SELECT name FROM system.tables WHERE database = {db:String} "
            "AND startsWith(name, 'events__chm_') ORDER BY name",
            parameters={"db": project.database},
        ).result_rows
    ]


def _locks(project):
    return project.client.query(
        "SELECT name, toString(uuid) FROM system.tables WHERE database = {db:String} "
        "AND name = '_chm_rebuild_lock_events'",
        parameters={"db": project.database},
    ).result_rows


def _lock_owner(project):
    locks = _locks(project)
    assert len(locks) == 1, locks
    rows = project.client.query(
        f"SELECT owner, max(heartbeat) FROM {project.database}._chm_rebuild_lock_events GROUP BY owner"
    ).result_rows
    assert len(rows) == 1, rows
    return {"owner": rows[0][0], "table_uuid": locks[0][1]}


def _lock_age(project):
    return project.client.command(
        f"SELECT dateDiff('second', max(heartbeat), now()) FROM {project.database}._chm_rebuild_lock_events"
    )


def _assert_locked_refusal(project, owner, orphan):
    original_uuid = _uuid(project)
    second = project.run("up", "it", "--timeout", project.budget(1))
    assert second.exit_code != 0, second.output
    assert "lock" in second.output.lower(), second.output
    assert "reconcil" in second.output.lower(), second.output
    assert _lock_owner(project) == owner
    assert _uuid(project) == original_uuid
    if _active_copy(project) != orphan:
        project.flush_logs()
        outcome = project.client.command(
            f"SELECT type FROM {project.query_log()} WHERE query_id = {{id:String}} "
            "ORDER BY event_time_microseconds DESC LIMIT 1",
            parameters={"id": orphan},
        )
        assert outcome == "QueryFinish", "Newcomer must not cancel the old copy"


def _operator_release(project, process, owner):
    assert process.poll() is not None, "Operator must first prove prior process cannot resume"
    assert _lock_owner(project) == owner, "Operator must reconcile unchanged lock ownership"
    assert _heads(project) == ["aaaa"], "Operator must reconcile migration journal/version"
    assert _active_copy(project), "Operator must account for server-side in-flight work"
    project.client.command(f"DROP TABLE {project.database}._chm_rebuild_lock_events SYNC")
    assert not _locks(project)


def _copy_after_moved_partition(project, partition):
    query_id = _active_copy(project)
    if query_id and _partition_rows(project, "events__chm_new", partition) == 200:
        return query_id
    return None


def _pending_mutation(project):
    return project.client.command(
        "SELECT count() FROM system.mutations WHERE database = {db:String} "
        "AND table = 'events' AND is_done = 0",
        parameters={"db": project.database},
    )


def _queued_async(project):
    return project.client.command(
        f"SELECT count() FROM {project.everywhere('system.asynchronous_inserts')} "
        "WHERE database = {db:String} AND table = 'events' AND length(entries.query_id) > 0",
        parameters={"db": project.database},
    )


def _active_copy(project):
    rows = project.client.query(
        f"SELECT query_id FROM {project.everywhere('system.processes')} "
        "WHERE startsWith(query_id, 'chm-rebuild-') "
        "AND query_kind = 'Insert' AND position(query, {db:String}) > 0",
        parameters={"db": project.database},
    ).result_rows
    return rows[0][0] if rows else None


def _uuid(project):
    return project.client.command(
        "SELECT toString(uuid) FROM system.tables WHERE database = {db:String} AND name = 'events'",
        parameters={"db": project.database},
    )


def _send_async0(project, query_id, row_id, client=None):
    sql = (
        f"INSERT INTO {project.database}.events (id, ts, k, payload) VALUES "
        f"({row_id}, '2026-02-02 00:00:00', 1, 'async')"
    )
    settings = {
        "query_id": query_id,
        "async_insert": 1,
        "wait_for_async_insert": 0,
        "async_insert_use_adaptive_busy_timeout": 0,
        "async_insert_busy_timeout_ms": 8000,
        "async_insert_busy_timeout_max_ms": 8000,
    }
    if not project.cloud:  # Cloud locks log_query_settings (already on there).
        settings["log_query_settings"] = 1
    (client or project.client).command(sql, settings=settings)


def _rebuild_result(project):
    rows = project.client.query(
        f"SELECT argMax(payload, sequence) FROM {project.database}._ch_migrate_journal "
        "WHERE revision = 'bbbb' AND position > 0 GROUP BY generation, position"
    ).result_rows
    records = [json.loads(row[0]) for row in rows]
    matching = [record for record in records if record.get("kind") == "rebuild"]
    assert len(matching) == 1, records
    assert matching[0]["phase"] == "done", matching[0]
    return matching[0]["result"]


def _until(predicate, message, seconds=30):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.05)
    pytest.fail(message)


@contextmanager
def _running(project, timeout="180"):
    path = project.root / f"rebuild-{time.monotonic_ns()}.log"
    with path.open("w") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "ch_migrate.cli", "up", "it", "--timeout", timeout],
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
            process.wait(timeout=15)


@contextmanager
def _hold_command(project, upstream, sql_prefix):
    blocked, release = threading.Event(), threading.Event()
    forwarder = _Upstream(upstream)

    class Forwarder(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            body = _request_body(self)
            query = parse_qs(urlsplit(self.path).query).get("query", [""])[0]
            if (query + body.decode("utf-8", errors="ignore")).lstrip().startswith(sql_prefix):
                blocked.set()
                if not release.wait(30):
                    self.send_error(504, "Test exchange barrier timed out")
                    return
            headers = {
                key: value
                for key, value in self.headers.items()
                if key.lower() not in {"host", "connection", "transfer-encoding", "content-length"}
            }
            headers["Content-Length"] = str(len(body))
            try:
                status, response_headers, payload = forwarder.forward(
                    self.command, self.path, (body, headers)
                )
                self.send_response(status)
                for key, value in response_headers:
                    if key.lower() not in {"content-length", "transfer-encoding", "connection"}:
                        self.send_header(key, value)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(payload)
                self.close_connection = True
            except (BrokenPipeError, ConnectionResetError):
                pass  # A deliberate SIGKILL can close the downstream client after server success.

        do_GET = do_POST

        def log_message(self, *args):
            pass  # Never log authentication-bearing request query strings.

    server = ThreadingHTTPServer(("127.0.0.1", 0), Forwarder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    config_path = project.root / "config.yaml"
    original = config_path.read_text()
    config = yaml.safe_load(original)
    config["environments"]["it"].update(host="127.0.0.1", port=server.server_port, secure=False)
    config_path.write_text(yaml.safe_dump(config))
    thread.start()
    try:
        yield blocked, release
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
        forwarder.close()
        config_path.write_text(original)


class _Upstream:
    """Forward to the real server, keeping one upstream connection per HTTP session.

    ClickHouse Cloud routes each connection, not each session id, to a replica, so a
    fresh connection per request could reach a replica without the session.
    """

    def __init__(self, server):
        self.server = server
        self.lock = threading.Lock()
        self.sessions = {}

    def forward(self, method, path, request):
        session = parse_qs(urlsplit(path).query).get("session_id", [None])[0]
        if session is None:
            connection = self._connect()
            try:
                return _exchange(connection, method, path, request)
            finally:
                connection.close()
        with self.lock:
            entry = self.sessions.setdefault(session, {"lock": threading.Lock(), "conn": None})
        with entry["lock"]:
            entry["conn"] = entry["conn"] or self._connect()
            try:
                return _exchange(entry["conn"], method, path, request)
            except (http.client.HTTPException, OSError):
                entry["conn"].close()
                entry["conn"] = None
                raise

    def close(self):
        for entry in self.sessions.values():
            if entry["conn"] is not None:
                entry["conn"].close()

    def _connect(self):
        if self.server.secure:
            return http.client.HTTPSConnection(
                self.server.host,
                self.server.port,
                timeout=120,
                context=ssl.create_default_context(cafile=certifi.where()),
            )
        return http.client.HTTPConnection(self.server.host, self.server.port, timeout=120)


def _exchange(connection, method, path, request):
    body, headers = request
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    return response.status, response.getheaders(), response.read()


def _request_body(handler):
    if handler.headers.get("Transfer-Encoding", "").lower() != "chunked":
        return handler.rfile.read(int(handler.headers.get("Content-Length", 0)))
    body = bytearray()
    while True:
        size = int(handler.rfile.readline().split(b";", 1)[0], 16)
        if size == 0:
            while handler.rfile.readline().strip():
                pass  # Consume optional HTTP trailers.
            return bytes(body)
        body.extend(handler.rfile.read(size))
        assert handler.rfile.read(2) == b"\r\n"
