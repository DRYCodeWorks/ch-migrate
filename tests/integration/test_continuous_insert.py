"""Acknowledgement, duplicate-window and loss accounting against owned servers."""

from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from uuid import uuid4

import pytest
from test_rebuild_commands import (
    _active_copy,
    _baseline,
    _copy_after_moved_partition,
    _heads,
    _helpers,
    _hold_command,
    _lock_age,
    _lock_owner,
    _locks,
    _operator_release,
    _rebuild_result,
    _revision,
    _running,
    _slow_projection,
    _target,
    _until,
    _uuid,
)
from test_rebuild_dependents import _server_intervals

pytestmark = pytest.mark.integration
_START_ID = 3000000
_BATCH_SIZE = 18


@pytest.fixture
def owned_project(request):
    if os.environ.get("CH_MIGRATE_IT_URL"):
        pytest.skip("Continuous acceptance only uses fixture-owned Docker servers")
    return request.getfixturevalue("project")


@pytest.mark.parametrize("mode", ["sync", "kill", "async1", "async0"])
def test_continuous_insert_single_node(owned_project, clickhouse_server, mode):
    project = owned_project
    _prepare(project, mode)
    writer = _exercise(project, clickhouse_server, mode)
    _assert_accounting(project, writer)


def test_continuous_insert_replicated(cluster_project, clickhouse_cluster):
    project, cluster = cluster_project, clickhouse_cluster
    engine = f"ReplicatedMergeTree('/chm-continuous/{project.database}', '{{replica}}')"
    project.client.command(
        f"CREATE TABLE {project.database}.events ON CLUSTER {cluster.name} "
        "(id UInt64, ts DateTime, k UInt8, payload String) "
        f"ENGINE = {engine} PARTITION BY toYYYYMM(ts) ORDER BY id"
    )
    _prepare(project, "replicated", engine)
    for client in cluster.clients.values():
        client.command(f"SYSTEM SYNC REPLICA {project.database}.events")
        client.command("SYSTEM FLUSH LOGS")
    writer = _exercise(project, cluster.servers[1], "replicated")
    reports = []
    for client in cluster.clients.values():
        client.command(f"SYSTEM SYNC REPLICA {project.database}.events")
        reports.append(_assert_accounting(replace(project, client=client), writer))
    assert reports[0] == reports[1]
    assert len(_rebuild_result(project)["new_uuids"]) == 2


@dataclass
class _Writer:
    mode: str
    stop: threading.Event = field(default_factory=threading.Event)
    long_flush: threading.Event = field(default_factory=threading.Event)
    gate_query: str | None = None
    attempts: dict = field(default_factory=dict)
    returned: list = field(default_factory=list)
    retries: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    intervals: dict = field(default_factory=dict)
    forced_ids: set = field(default_factory=set)
    seed_rows: int = 6000

    def arm(self):
        self.gate_query = None
        self.long_flush.set()


def _prepare(project, mode, engine="MergeTree()"):
    if mode != "replicated":
        project.client.command(
            f"CREATE TABLE {project.database}.events "
            "(id UInt64, ts DateTime, k UInt8, payload String) "
            f"ENGINE = {engine} PARTITION BY toYYYYMM(ts) ORDER BY id"
        )
    _baseline(project)
    project.client.command(
        f"INSERT INTO {project.database}.events SELECT number, toDateTime('2026-01-01') + number, "
        "number % 7, toString(number) FROM numbers(200)"
    )
    rows = 120000 if mode == "kill" else 6000
    project.client.command(
        f"INSERT INTO {project.database}.events SELECT 100000 + number, "
        "toDateTime('2026-02-01') + number, number % 7, toString(100000 + number) "
        f"FROM numbers({rows})"
    )
    _revision(project, target=_target(project, engine), select=_slow_projection())
    project.client.command("SYSTEM FLUSH LOGS")


def _exercise(project, server, mode):
    writer = _Writer(mode, seed_rows=120000 if mode == "kill" else 6000)
    with _writing(server, project, writer):
        _until(lambda: len(writer.returned) >= 3, "Writer did not start")
        if mode == "async0":
            _assert_async_refusal(project, writer)
            _revision(project, select=_slow_projection(), allow_async_loss=True)
        if mode == "async1":
            # Keep a real waiting INSERT visible across both preflight reads.
            # Its settings must be read from processes before query_log flushes.
            writer.arm()
            _until(lambda: _gate_queued(project, writer), "Waiting insert did not queue")
        snapshot = f"ALTER TABLE `{project.database}`.`events__chm_snap` ATTACH PARTITION"
        prefixes = (snapshot, "EXCHANGE TABLES") if mode.startswith("async") else snapshot
        with _hold_command(project, server, prefixes) as gate:
            _rebuild_with_writer(project, writer, gate)
        after = len(writer.returned)
        _until(lambda: len(writer.returned) >= after + 3, "Writer did not continue after swap")
    assert not writer.errors, writer.errors
    project.client.command(f"SYSTEM FLUSH ASYNC INSERT QUEUE {project.database}.events")
    assert _heads(project) == ["bbbb"] and not _locks(project)
    window = _rebuild_result(project)["duplicate_window"]
    writer.intervals = _insert_times(project, writer, window)
    return writer


def _assert_async_refusal(project, writer):
    writer.arm()
    _until(lambda: _gate_queued(project, writer), "No unacknowledged batch queued")
    project.client.command("SYSTEM FLUSH LOGS")
    refused = project.run("up", "it", "--timeout", "20")
    assert refused.exit_code != 0 and "async" in refused.output.lower(), refused.output
    assert not _helpers(project) and not _locks(project)
    project.client.command(f"SYSTEM FLUSH ASYNC INSERT QUEUE {project.database}.events")


def _rebuild_with_writer(project, writer, gate):
    blocked, release = gate
    # Keep process ownership and both real HTTP barriers in one cleanup scope.
    with _running(project) as (process, log):
        assert blocked.wait(30), log.read_text()
        before = len(writer.returned)
        _until(lambda: len(writer.returned) >= before + 3, "No batch entered the snapshot window")
        writer.forced_ids = {
            row_id for query_id in writer.returned[before:] for row_id in writer.attempts[query_id]
        }
        if writer.mode == "async0":
            project.client.command(f"SYSTEM FLUSH ASYNC INSERT QUEUE {project.database}.events")
        blocked.clear()
        release.set()
        if writer.mode == "kill":
            _interrupt_and_resume(project, process)
        elif writer.mode.startswith("async"):
            _until(lambda: _active_copy(project), "Copy never started")
            blocked.clear()
            release.clear()
            assert blocked.wait(90), log.read_text()
            writer.arm()
            _until(lambda: _gate_queued(project, writer), "No old-UUID async batch queued")
            release.set()
        if writer.mode != "kill":
            assert process.wait(timeout=180) == 0, log.read_text()


def _interrupt_and_resume(project, process):
    _until(
        lambda: _copy_after_moved_partition(project, "202601"),
        "First partition was not checkpointed before copying the second",
        seconds=90,
    )
    owner, source_uuid = _lock_owner(project), _uuid(project)
    process.kill()
    assert process.wait(timeout=15) != 0
    _until(lambda: _lock_age(project) >= 9, "Killed owner's heartbeat did not expire")
    refused = project.run("up", "it", "--timeout", "1")
    assert refused.exit_code != 0 and "reconcil" in refused.output.lower(), refused.output
    assert _uuid(project) == source_uuid and _lock_owner(project) == owner
    _operator_release(project, process, owner)
    with _running(project) as (resumed, log):
        assert resumed.wait(timeout=180) == 0, log.read_text()
    project.client.command("SYSTEM FLUSH LOGS")
    completed = project.client.command(
        "SELECT count() FROM system.query_log WHERE type = 'QueryFinish' "
        "AND startsWith(query_id, 'chm-rebuild-') AND endsWith(query_id, '202601') "
        "AND position(query, {db:String}) > 0",
        parameters={"db": project.database},
    )
    assert completed == 1, "Resume recopied the checkpointed partition"


@contextmanager
def _writing(server, project, writer):
    def send():
        client = server.connect()
        try:
            batch = 0
            while not writer.stop.is_set():
                _send_batch(client, project, (writer, batch))
                batch += 1
                writer.stop.wait(0.03)
        except Exception as exc:
            writer.errors.append(exc)
        finally:
            client.close()

    thread = threading.Thread(target=send, daemon=True)
    thread.start()
    try:
        yield
    finally:
        writer.stop.set()
        thread.join(timeout=30)
        assert not thread.is_alive(), "Writer did not stop"


def _send_batch(client, project, batch):
    writer, number = batch
    ids = tuple(range(_START_ID + number * _BATCH_SIZE, _START_ID + (number + 1) * _BATCH_SIZE))
    values = ", ".join(
        f"({row_id}, '2026-0{2 + row_id % 3}-02', {row_id % 7}, '{row_id}')" for row_id in ids
    )
    for attempt in range(3):
        query_id = "chm-continuous-" + uuid4().hex
        writer.attempts[query_id] = ids
        settings = _writer_settings(writer, query_id)
        try:
            client.command(
                f"INSERT INTO {project.database}.events (id, ts, k, payload) VALUES {values}",
                settings=settings,
            )
        except Exception as exc:
            if writer.mode != "async1" or "Code: 741." not in str(exc) or attempt == 2:
                raise
            writer.retries.append(query_id)
            continue
        writer.returned.append(query_id)
        return


def _writer_settings(writer, query_id):
    delayed = writer.long_flush.is_set()
    if delayed:
        writer.long_flush.clear()
        writer.gate_query = query_id
    return {
        "query_id": query_id,
        "log_queries": 1,
        "log_query_settings": 1,
        "async_insert": int(writer.mode.startswith("async")),
        "wait_for_async_insert": int(writer.mode != "async0"),
        "async_insert_use_adaptive_busy_timeout": 0,
        "async_insert_busy_timeout_ms": 8000 if delayed else 50,
        "async_insert_busy_timeout_max_ms": 8000 if delayed else 50,
    }


def _gate_queued(project, writer):
    if writer.gate_query is None:
        return False
    return project.client.command(
        "SELECT count() FROM system.asynchronous_inserts WHERE database = {db:String} "
        "AND table = 'events' AND has(entries.query_id, {query:String})",
        parameters={"db": project.database, "query": writer.gate_query},
    )


def _insert_times(project, writer, window):
    if not writer.mode.startswith("async"):
        return _server_intervals(project, writer.returned, window)
    project.client.command("SYSTEM FLUSH LOGS")
    rows = project.client.query(
        "SELECT query_id, status, flush_time_microseconds >= toDateTime64({start:String}, 6) "
        "AND flush_time_microseconds <= toDateTime64({end:String}, 6) "
        "FROM system.asynchronous_insert_log WHERE database = {db:String} "
        "AND table = 'events' AND has({ids:Array(String)}, query_id)",
        parameters={"db": project.database, "ids": list(writer.attempts), **window},
    ).result_rows
    evidence = {query_id: (status, bool(inside)) for query_id, status, inside in rows}
    assert len(evidence) == len(rows) and set(evidence) == set(writer.attempts)
    assert all(status in ("Ok", "FlushError") for status, _ in evidence.values())
    errors = {query_id for query_id, (status, _) in evidence.items() if status == "FlushError"}
    report = _rebuild_result(project)["async_flush_errors"]
    assert errors == set(report["query_ids"]) and len(errors) == report["count"]
    assert writer.gate_query in errors, "The real old-UUID flush race was not exercised"
    if writer.mode == "async1":
        assert errors == set(writer.retries), "Every 741 must be retried on the new table"
        assert not errors.intersection(writer.returned)
    return {query_id: inside for query_id, (status, inside) in evidence.items() if status == "Ok"}


def _assert_accounting(project, writer):
    rows = project.client.query(
        f"SELECT id, count(), groupUniqArray(k), groupUniqArray(payload) "
        f"FROM {project.database}.events WHERE id >= {_START_ID} GROUP BY id"
    ).result_rows
    actual = {row_id for row_id, _, _, _ in rows}
    sent = {row_id for query_id in writer.returned for row_id in writer.attempts[query_id]}
    assert len(sent) == len(writer.returned) * _BATCH_SIZE
    assert actual.issubset(sent)
    lost = sent - actual
    _assert_loss(project, writer, lost)
    owners = {
        row_id: query_id for query_id in writer.returned for row_id in writer.attempts[query_id]
    }
    duplicates = {row_id for row_id, count, _, _ in rows if count > 1}
    assert duplicates.intersection(writer.forced_ids), "Snapshot overlap was not exercised"
    assert all(writer.intervals[owners[row_id]] for row_id in duplicates)
    assert all(count in (1, 2) for _, count, _, _ in rows)
    assert all(
        keys == [row_id % 7] and payloads == [str(row_id)] for row_id, _, keys, payloads in rows
    )
    _assert_seed(project, writer.seed_rows)
    assert "ORDER BY (k, ts, id)" in project.client.command(
        f"SHOW CREATE TABLE {project.database}.events"
    )
    report = {
        "mode": writer.mode,
        "sent": len(sent),
        "duplicates": len(duplicates),
        "lost": len(lost),
        "retries_741": len(writer.retries),
    }
    print("CONTINUOUS_INSERT " + json.dumps(report, sort_keys=True), flush=True)
    return report


def _assert_loss(project, writer, lost):
    if writer.mode != "async0":
        assert not lost, f"Lost {len(lost)} acknowledged IDs"
        return
    errors = _rebuild_result(project)["async_flush_errors"]
    reported = {row_id for query_id in errors["query_ids"] for row_id in writer.attempts[query_id]}
    assert lost == reported and lost, "Every lost row must match an old-UUID FlushError"
    assert len(lost) == errors["count"] * _BATCH_SIZE


def _assert_seed(project, rows):
    actual = project.client.query(
        f"SELECT id, count(), any(payload) FROM {project.database}.events "
        f"WHERE id < {_START_ID} GROUP BY id"
    ).result_rows
    expected = set(range(200)) | set(range(100000, 100000 + rows))
    assert {row_id for row_id, _, _ in actual} == expected
    assert all(count == 1 and payload == str(row_id) for row_id, count, payload in actual)
