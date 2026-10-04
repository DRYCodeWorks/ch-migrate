"""Real-server dependent behavior during an owned online table rebuild."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from uuid import uuid4

import pytest
from test_rebuild_commands import _hold_command

pytestmark = pytest.mark.integration


@pytest.fixture
def owned_project(request):
    if os.environ.get("CH_MIGRATE_IT_URL"):
        pytest.skip("Dependent acceptance requires the fixture-owned Docker server")
    return request.getfixturevalue("project")


def test_live_aggregate_and_dictionary_survive_rebuild(owned_project, clickhouse_server, request):
    project = owned_project
    _source(project)
    _aggregate(project)
    _dictionary(project, request)
    _baseline(project)
    _seed(project)
    db = project.database
    initial = _totals(project)
    assert initial == _aggregate_totals(project)
    assert project.client.command(f"SELECT dictGetUInt64('{db}.events_dict', 'value', 1)") == 1
    assert project.client.command(f"SELECT dictHas('{db}.events_dict', 2000000)") == 0
    _revision(project)
    acknowledged = []
    with _writer(clickhouse_server, project, acknowledged) as errors:
        _until(lambda: len(acknowledged) >= 16, "Writer never acknowledged its first batch")
        prefix = f"ALTER TABLE `{db}`.`events__chm_snap` ATTACH PARTITION"
        with _hold_command(project, clickhouse_server, prefix) as (blocked, release):
            with _running(project) as (process, log):
                assert blocked.wait(30), log.read_text()
                acknowledged_before = len(acknowledged)
                _until(
                    lambda: len(acknowledged) > acknowledged_before,
                    "No insert committed inside the held snapshot window",
                )
                forced_ids = {
                    row_id
                    for _, batch in acknowledged[acknowledged_before:]
                    for row_id, _, _ in batch
                }
                release.set()
                assert process.wait(timeout=180) == 0, log.read_text()
    assert not errors, errors
    assert acknowledged
    result = _result(project)
    duplicates = _assert_accounting(project, acknowledged, initial, result["duplicate_window"])
    assert forced_ids and forced_ids.issubset(duplicates)
    assert project.client.command(f"SELECT dictHas('{db}.events_dict', 2000000)") == 1
    assert (
        project.client.command(f"SELECT dictGetUInt64('{db}.events_dict', 'value', 2000000)") == 1
    )
    aggregate_before = _aggregate_totals(project)
    source_before = _totals(project)
    project.client.command(f"INSERT INTO {db}.events VALUES (8000000, '2026-02-02', 1, 43)")
    assert _aggregate_totals(project)[1] == aggregate_before[1] + 43
    assert _totals(project)[1] == source_before[1] + 43
    assert _heads(project) == ["bbbb"]


def test_dictionary_reload_reads_the_replacement_storage(owned_project, clickhouse_server, request):
    project = owned_project
    _source(project)
    _dictionary(project, request)
    _baseline(project)
    _seed(project, 100)
    db = project.database
    old_uuid = _uuid(project)
    assert project.client.command(f"SELECT dictHas('{db}.events_dict', 9000000)") == 0
    _revision(project)
    with _hold_command(project, clickhouse_server, "SYSTEM RELOAD DICTIONARY") as (
        blocked,
        release,
    ):
        with _running(project) as (process, log):
            assert blocked.wait(30), log.read_text()
            assert _uuid(project) != old_uuid
            try:
                project.client.command(
                    f"INSERT INTO {db}.events VALUES (9000000, '2026-02-02', 1, 777)"
                )
                assert (
                    project.client.command(
                        f"SELECT count() FROM {db}.events__chm_old_bbbb WHERE id=9000000"
                    )
                    == 0
                )
            finally:
                release.set()
            assert process.wait(timeout=90) == 0, log.read_text()
    assert (
        project.client.command(f"SELECT dictGetUInt64('{db}.events_dict', 'value', 9000000)") == 777
    )
    assert _heads(project) == ["bbbb"]


@pytest.mark.parametrize("change", ["drop", "rename", "type"])
def test_invalid_source_view_refuses_then_repaired_sql_succeeds(owned_project, change):
    project = owned_project
    _source(project)
    _aggregate(project)
    _baseline(project)
    _seed(project, 200)
    before = (_uuid(project), _source_rows(project), _aggregate_totals(project))
    projection = {
        "drop": "id, ts, k",
        "rename": "id, ts, k, value AS renamed_value",
        "type": "id, ts, k, toString(value) AS value",
    }[change]
    _revision(project, _invalid_target(change), projection)
    rejected = project.run("up", "it", "--timeout", "15")
    assert rejected.exit_code != 0, rejected.output
    assert "events_mv" in rejected.output, rejected.output
    assert (_uuid(project), _source_rows(project), _aggregate_totals(project)) == before
    assert not _helpers(project) and not _locks(project)
    assert _heads(project) == ["aaaa"]
    _revision(project)
    repaired = project.run("up", "it", "--timeout", "180")
    assert repaired.exit_code == 0, repaired.output
    assert _uuid(project) != before[0]
    assert _source_rows(project) == before[1]
    assert _aggregate_totals(project) == before[2]
    assert _heads(project) == ["bbbb"]


@pytest.mark.parametrize("qualifier", ["none", "table", "database_table"])
def test_inner_engine_materialized_view_keeps_receiving_rows(owned_project, qualifier):
    project = owned_project
    _source(project)
    db = project.database
    prefix = {"none": "", "table": "events.", "database_table": f"{db}.events."}[qualifier]
    project.client.command(
        f"CREATE MATERIALIZED VIEW {db}.inner_mv ENGINE = SummingMergeTree() ORDER BY k "
        f"AS SELECT {prefix}k AS k, sum({prefix}value) AS value FROM {db}.events GROUP BY {prefix}k"
    )
    _baseline(project)
    _seed(project, 200)
    before = _inner_totals(project)
    assert before == _totals(project)
    _revision(project)
    applied = project.run("up", "it", "--timeout", "180")
    assert applied.exit_code == 0, applied.output
    assert _inner_totals(project) == before
    project.client.command(f"INSERT INTO {db}.events VALUES (500000, '2026-02-02', 1, 37)")
    assert _inner_totals(project) == _totals(project)
    assert _inner_totals(project)[1] == before[1] + 37


@pytest.mark.parametrize("feed_id_type", ["UInt64", "UInt32"])
def test_materialized_view_to_rebuilt_table_uses_new_target(owned_project, feed_id_type):
    project = owned_project
    _source(project)
    db = project.database
    project.client.command(
        f"CREATE TABLE {db}.feed (id {feed_id_type}, ts DateTime, k UInt8, value UInt64) "
        "ENGINE = MergeTree() ORDER BY id"
    )
    project.client.command(
        f"CREATE MATERIALIZED VIEW {db}.feed_mv TO {db}.events "
        f"AS SELECT id, ts, k, value FROM {db}.feed"
    )
    _baseline(project)
    project.client.command(f"INSERT INTO {db}.feed VALUES (1, '2026-02-02', 1, 17)")
    assert _source_rows(project) == [(1, 1, 17)]
    before_uuid = _uuid(project)
    _revision(project)
    applied = project.run("up", "it", "--timeout", "180")
    assert applied.exit_code == 0, applied.output
    assert _uuid(project) != before_uuid
    project.client.command(f"INSERT INTO {db}.feed VALUES (2, '2026-02-02', 2, 31)")
    assert _source_rows(project) == [(1, 1, 17), (2, 2, 31)]
    assert _heads(project) == ["bbbb"]


def test_single_shard_distributed_route_is_conservatively_refused(owned_project):
    project = owned_project
    db = project.database
    cluster = project.client.query(
        "SELECT cluster FROM system.clusters GROUP BY cluster "
        "HAVING uniqExact(shard_num) = 1 AND count() = 1 LIMIT 1"
    ).result_rows
    assert cluster, "Owned server has no single-node Distributed cluster"
    _source(project)
    _baseline(project)
    project.client.command(
        f"CREATE TABLE {db}.events_router (id UInt64, ts DateTime, k UInt8, value UInt64) "
        f"ENGINE = Distributed('{cluster[0][0]}', '{db}', 'events', id)"
    )
    project.client.command(f"INSERT INTO {db}.events VALUES (1, '2026-02-02', 1, 9)")
    before = (_uuid(project), _source_rows(project))
    _revision(project)
    rejected = project.run("up", "it", "--timeout", "15")
    assert rejected.exit_code != 0, rejected.output
    assert "Distributed" in rejected.output
    assert (_uuid(project), _source_rows(project)) == before
    assert not _helpers(project) and not _locks(project)
    assert _heads(project) == ["aaaa"]


def _source(project):
    project.client.command(
        f"CREATE TABLE {project.database}.events "
        "(id UInt64, ts DateTime, k UInt8, value UInt64) "
        "ENGINE = MergeTree() PARTITION BY toYYYYMM(ts) ORDER BY id"
    )


def _aggregate(project):
    db = project.database
    project.client.command(
        f"CREATE TABLE {db}.totals (k UInt8, value UInt64, rows UInt64) "
        "ENGINE = SummingMergeTree() ORDER BY k"
    )
    project.client.command(
        f"CREATE MATERIALIZED VIEW {db}.events_mv TO {db}.totals AS "
        f"SELECT k, sum(value) AS value, count() AS rows FROM {db}.events GROUP BY k"
    )


def _dictionary(project, request):
    db = project.database
    reader = "reader_" + db
    project.client.command(f"CREATE USER {reader} IDENTIFIED WITH no_password HOST LOCAL")
    request.addfinalizer(lambda: project.client.command(f"DROP USER IF EXISTS {reader}"))
    project.client.command(f"GRANT SELECT ON {db}.events TO {reader}")
    project.client.command(
        f"CREATE DICTIONARY {db}.events_dict (id UInt64, value UInt64) "
        f"PRIMARY KEY id SOURCE(CLICKHOUSE(USER '{reader}' DB '{db}' TABLE 'events')) "
        "LIFETIME(MIN 0 MAX 0) LAYOUT(HASHED())"
    )


def _baseline(project):
    project.write_revision("aaaa", {"upgrade": 'op.execute("SELECT 1")'})
    result = project.run("up", "it")
    assert result.exit_code == 0, result.output


def _seed(project, rows=12000):
    project.client.command(
        f"INSERT INTO {project.database}.events "
        "SELECT number + 1, toDateTime('2026-02-01'), number % 7, "
        f"number % 23 + 1 FROM numbers({rows})"
    )
    project.client.command("SYSTEM FLUSH LOGS")


def _revision(project, target=None, select=None):
    (project.sql_dir / "rebuild.sql").write_text(target or _target())
    projection = select or "id, ts, k, value"
    project.write_revision(
        "bbbb",
        {"upgrade": f"op.rebuild_table('events', 'rebuild.sql', select={projection!r})"},
        "aaaa",
    )


def _target():
    return (
        "CREATE TABLE {db}.events (id UInt64, ts DateTime, k UInt8, value UInt64) "
        "ENGINE = MergeTree() PARTITION BY toYYYYMM(ts) ORDER BY (k, ts, id)"
    )


def _invalid_target(change):
    columns = {
        "drop": "id UInt64, ts DateTime, k UInt8",
        "rename": "id UInt64, ts DateTime, k UInt8, renamed_value UInt64",
        "type": "id UInt64, ts DateTime, k UInt8, value String",
    }
    return (
        f"CREATE TABLE {{db}}.events ({columns[change]}) ENGINE = MergeTree() "
        "PARTITION BY toYYYYMM(ts) ORDER BY (k, ts, id)"
    )


def _uuid(project):
    return project.client.command(
        "SELECT toString(uuid) FROM system.tables "
        "WHERE database = {db:String} AND name = 'events'",
        parameters={"db": project.database},
    )


def _source_rows(project):
    return project.client.query(
        f"SELECT id, k, value FROM {project.database}.events ORDER BY id, k, value"
    ).result_rows


def _totals(project):
    return dict(
        project.client.query(
            f"SELECT k, sum(value) FROM {project.database}.events GROUP BY k ORDER BY k"
        ).result_rows
    )


def _aggregate_totals(project):
    return dict(
        project.client.query(
            f"SELECT k, sum(value) FROM {project.database}.totals GROUP BY k ORDER BY k"
        ).result_rows
    )


def _aggregate_counts(project):
    return dict(
        project.client.query(
            f"SELECT k, sum(rows) FROM {project.database}.totals GROUP BY k ORDER BY k"
        ).result_rows
    )


def _inner_totals(project):
    return dict(
        project.client.query(
            f"SELECT k, sum(value) FROM {project.database}.inner_mv GROUP BY k ORDER BY k"
        ).result_rows
    )


def _helpers(project):
    return project.client.query(
        "SELECT name FROM system.tables WHERE database = {db:String} "
        "AND startsWith(name, 'events__chm_') ORDER BY name",
        parameters={"db": project.database},
    ).result_rows


def _locks(project):
    return project.client.query(
        "SELECT name FROM system.tables WHERE database = {db:String} "
        "AND name = '_chm_rebuild_lock_events'",
        parameters={"db": project.database},
    ).result_rows


def _heads(project):
    return [
        row[0]
        for row in project.client.query(
            f"SELECT version_num FROM {project.database}.alembic_version ORDER BY version_num"
        ).result_rows
    ]


def _result(project):
    rows = project.client.query(
        f"SELECT argMax(payload, sequence) FROM {project.database}._ch_migrate_journal "
        "WHERE revision = 'bbbb' AND position > 0 GROUP BY generation, position"
    ).result_rows
    records = [json.loads(row[0]) for row in rows]
    matching = [item for item in records if item.get("kind") == "rebuild"]
    assert len(matching) == 1, records
    assert matching[0]["phase"] == "done", matching[0]
    return matching[0]["result"]


def _assert_accounting(project, acknowledged, initial, window):
    expected = {row_id: (key, value) for _, batch in acknowledged for row_id, key, value in batch}
    assert len(expected) == sum(len(batch) for _, batch in acknowledged)
    actual = project.client.query(
        f"SELECT id, any(k), any(value), count() FROM {project.database}.events "
        "WHERE id >= 2000000 GROUP BY id"
    ).result_rows
    assert {row_id: (key, value) for row_id, key, value, _ in actual} == expected
    assert all(count in (1, 2) for _, _, _, count in actual)
    duplicates = {row_id: (value, count - 1) for row_id, _, value, count in actual if count > 1}
    intervals = _server_intervals(project, [query_id for query_id, _ in acknowledged], window)
    owners = {row_id: query_id for query_id, batch in acknowledged for row_id, _, _ in batch}
    assert all(intervals[owners[row_id]] for row_id in duplicates)
    excess_by_key = {key: 0 for key in initial}
    for row_id, (value, repeats) in duplicates.items():
        key = expected[row_id][0]
        excess_by_key[key] += value * repeats
    expected_once = dict(initial)
    expected_counts = dict(
        project.client.query(
            f"SELECT k, count() FROM {project.database}.{_result(project)['rollback_table']} "
            "WHERE id < 2000000 GROUP BY k"
        ).result_rows
    )
    for _, (key, value) in expected.items():
        expected_once[key] = expected_once.get(key, 0) + value
        expected_counts[key] = expected_counts.get(key, 0) + 1
    aggregate = _aggregate_totals(project)
    assert aggregate == expected_once
    assert _aggregate_counts(project) == expected_counts
    assert {key: _totals(project)[key] - aggregate[key] for key in aggregate} == excess_by_key
    return duplicates


def _server_intervals(project, query_ids, window):
    project.client.command("SYSTEM FLUSH LOGS")
    rows = project.client.query(
        "SELECT query_id, countIf(type = 'QueryStart'), countIf(type = 'QueryFinish'), "
        "minIf(event_time_microseconds, type = 'QueryStart') <= "
        "parseDateTime64BestEffort({end:String}, 6) AND "
        "maxIf(event_time_microseconds, type = 'QueryFinish') >= "
        "parseDateTime64BestEffort({start:String}, 6) AS overlaps "
        "FROM system.query_log WHERE has({ids:Array(String)}, query_id) "
        "AND type IN ('QueryStart', 'QueryFinish') GROUP BY query_id",
        parameters={"ids": query_ids, "start": window["start"], "end": window["end"]},
    ).result_rows
    evidence = {
        query_id: (starts, finishes, bool(overlap)) for query_id, starts, finishes, overlap in rows
    }
    assert set(evidence) == set(query_ids)
    assert all(starts == 1 and finishes == 1 for starts, finishes, _ in evidence.values())
    return {query_id: overlap for query_id, (_, _, overlap) in evidence.items()}


def _until(predicate, message, seconds=30):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    pytest.fail(message)


@contextmanager
def _writer(server, project, acknowledged):
    stop = threading.Event()
    errors = []

    def send():
        client = server.connect()
        try:
            batch_number = 0
            while not stop.is_set():
                batch = [(2000000 + batch_number * 16 + n, n % 7, n % 23 + 1) for n in range(16)]
                values = ", ".join(
                    f"({row_id}, '2026-02-02', {key}, {value})" for row_id, key, value in batch
                )
                query_id = f"chm-deps-writer-{uuid4().hex}"
                try:
                    client.command(
                        f"INSERT INTO {project.database}.events (id, ts, k, value) VALUES {values}",
                        settings={
                            "query_id": query_id,
                            "log_queries": 1,
                            "log_query_settings": 1,
                            "async_insert": 0,
                            "wait_for_async_insert": 1,
                        },
                    )
                except Exception as exc:
                    errors.append(exc)
                    break
                acknowledged.append((query_id, batch))
                batch_number += 1
                stop.wait(0.03)
        finally:
            client.close()

    thread = threading.Thread(target=send, daemon=True)
    thread.start()
    try:
        yield errors
    finally:
        stop.set()
        thread.join(timeout=30)
        assert not thread.is_alive(), "Synchronous writer did not stop"


@contextmanager
def _running(project):
    path = project.root / f"dependent-rebuild-{uuid4().hex}.log"
    with path.open("w") as output:
        process = subprocess.Popen(
            [sys.executable, "-m", "ch_migrate.cli", "up", "it", "--timeout", "180"],
            cwd=project.root,
            env=os.environ.copy(),
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            yield process, path
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=15)
