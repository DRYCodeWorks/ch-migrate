"""Explicit reproduction of archived A/D SQL and the unchanged E prototype."""

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import clickhouse_connect
import pytest

from ch_migrate.sql import split_statements

SPIKE = Path(__file__).parents[2] / "docs/design/spikes/2026-10-02-rebuild"
pytestmark = pytest.mark.integration


def test_rebuild_spike_a_view_names_survive_exchange(project):
    script = (SPIKE / "a_exchange_mv.sql").read_text()
    for statement in split_statements(script):
        project.client.raw_query(statement.sql)
    source = project.client.query("SELECT id FROM a.t ORDER BY id").result_rows
    old = project.client.query("SELECT id FROM a.t_new ORDER BY id").result_rows
    sink = project.client.query("SELECT id FROM a.sink_src ORDER BY id").result_rows
    print("A source/old/sink", source, old, sink)
    assert source == sink == [(1,), (2,), (3,), (5,)]
    assert old == [(1,), (2,), (3,), (4,), (5,)]


@pytest.mark.parametrize(
    "script",
    [
        "d1_inflight_mv.sh",
        "d2_async_and_detect.sh",
        "d4_inflight_exchange.sh",
        "d5_async_exchange.sh",
    ],
)
def test_rebuild_spike_d_original_shell_sql(project, script):
    result = _run_archived_shell(project, script)
    assert result.returncode == 0, result.stdout + result.stderr
    print(script, result.stdout)
    if script.startswith("d1"):
        assert _counts(project.client, "d") == (20010, 10)
    elif script.startswith("d2"):
        assert _counts(project.client, "d2") == (6003, 6003)
        project.client.command("SYSTEM FLUSH LOGS")
        rows = project.client.query(
            "SELECT tables FROM system.query_log WHERE query_id = 'd3-cascade' AND type = 'QueryStart'"
        ).result_rows
        assert rows and "d2.feeder" in rows[0][0] and "d2.t" in rows[0][0]
    elif script.startswith("d4"):
        assert _counts(project.client, "d4") == (15000, 15000)
        assert project.client.command("SELECT uniqExact(id) FROM d4.sink") == 15000
    else:
        assert _counts(project.client, "d5") == (0, 0)
        assert project.client.command("SELECT count() FROM d5.slow_dst") == 6
        errors = project.client.query(
            "SELECT status, rows, exception FROM system.asynchronous_insert_log WHERE database = 'd5' AND table = 't'"
        ).result_rows
        assert sum(row[0] == "FlushError" for row in errors) == 2
        assert all("741" in row[2] for row in errors if row[0] == "FlushError")


@pytest.mark.parametrize("trial", [1, 2])
def test_rebuild_spike_e_original_prototype(project, clickhouse_server, trial):
    # Setup, writers, rebuild and finalization share the prototype's server state.
    module, clients, trace = _prototype(project, clickhouse_server)
    try:
        module.setup()
        logs = [project.root / f"writer_{kind}.jsonl" for kind in ("sync", "async", "async0")]
        rebuild_log = project.root / "rebuild.jsonl"
        with ThreadPoolExecutor(max_workers=3) as workers:
            futures = [
                workers.submit(module.writer, kind, first, str(path))
                for kind, first, path in zip(
                    ("sync", "async", "async0"), (10000000, 50000000, 90000000), logs
                )
            ]
            try:
                _wait_for_writers(logs)
                module.rebuild(str(rebuild_log))
                time.sleep(1)
            finally:
                Path(module.STOP).touch()
                for future in futures:
                    future.result(timeout=90)
        # A queued fire-and-forget batch is not a lost row; flush before measuring.
        project.client.command(f"SYSTEM FLUSH ASYNC INSERT QUEUE {project.database}.t")
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            module.verify([str(path) for path in [*logs, rebuild_log]])
        report = json.JSONDecoder().raw_decode(captured.getvalue())[0]
        print("E trial", trial, captured.getvalue())
        assert report["preload_rows"] == 6000000
        assert report["preload_missing"] == report["preload_duplicated"] == 0
        assert report["missing_acked_ids_by_writer"].get("sync", 0) == 0
        assert report["missing_acked_ids_by_writer"].get("async", 0) == 0
        server_report = _server_duplicate_report(project, trace, logs)
        print("E server-time evidence", trial, json.dumps(server_report, sort_keys=True))
        assert server_report["duplicate_ids_outside_window"] == 0
        assert server_report["unattributed_duplicate_ids"] == 0
        assert report["dependent_mv_sink_ids_not_exactly_once"] == 0
        assert (
            project.client.command(
                f"SELECT sorting_key FROM system.tables WHERE database = '{project.database}' AND name = 't'"
            )
            == "k, ts, id"
        )
    finally:
        for client in clients:
            client.close()


class _TracingClient:
    """Pass through real requests, adding IDs solely for server-time attribution."""

    def __init__(self, client, trace, kind):
        self.client, self.trace, self.kind = client, trace, kind

    def __getattr__(self, name):
        return getattr(self.client, name)

    def command(self, query, **kwargs):
        if query.startswith(f"CREATE MATERIALIZED VIEW {self.trace['database']}.t_dual "):
            kwargs["settings"] = {**kwargs.get("settings", {}), "query_id": self.trace["create_id"]}
        return self.client.command(query, **kwargs)

    def insert(self, table, data, **kwargs):
        if self.kind:
            key = (self.kind, data[0][0])
            query_id = f"{self.trace['prefix']}-{self.kind}-{data[0][0]}"
            self.trace["queries"][key] = query_id
            kwargs["settings"] = {**kwargs.get("settings", {}), "query_id": query_id}
        return self.client.insert(table, data, **kwargs)


def _run_archived_shell(project, script):
    environment = os.environ.copy()
    environment.update(
        {"SPIKE_PYTHON": sys.executable, "SPIKE_CLIENT": str(SPIKE / "reproduce_client.py")}
    )
    # Only this exact historical invocation is adapted; no Docker command executes.
    wrapper = """docker() {
      if [ "$1" != exec ] || [ "$2" != spike-rebuild-ch ] || [ "$3" != clickhouse-client ]; then
        echo "Refusing an unexpected archived Docker command" >&2; return 2
      fi
      shift 3
      "$SPIKE_PYTHON" "$SPIKE_CLIENT" "$@"
    }
    export -f docker
    bash "$1"
    """
    return subprocess.run(
        ["bash", "-c", wrapper, "spike", str(SPIKE / script)],
        cwd=project.root,
        env=environment,
        text=True,
        capture_output=True,
        timeout=120,
    )


def _counts(client, database):
    return client.query(
        f"SELECT (SELECT count() FROM {database}.t), (SELECT count() FROM {database}.t_new)"
    ).result_rows[0]


def _prototype(project, server):
    spec = importlib.util.spec_from_file_location("owned_rebuild_prototype", SPIKE / "e2e.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    clients = []
    trace = {"database": project.database, "prefix": f"spike-{project.database}", "queries": {}}
    trace["create_id"] = trace["prefix"] + "-view"
    original_log = module.log

    def record_phase(fh, **values):
        if values.get("step") == 3 and values.get("phase") == "end":
            trace["snapshot_end"] = project.client.command(
                "SELECT toUnixTimestamp64Micro(now64(6))"
            )
        original_log(fh, **values)

    module.log = record_phase

    def connect(**settings):
        client = clickhouse_connect.get_client(
            host=server.host,
            port=server.port,
            username=server.user,
            password=server.password,
            secure=server.secure,
            settings=settings,
            send_receive_timeout=60,
        )
        clients.append(client)
        kind = None
        if "async_insert" in settings:
            kind = (
                "sync"
                if not settings["async_insert"]
                else "async" if settings["wait_for_async_insert"] else "async0"
            )
        return _TracingClient(client, trace, kind)

    module.client = connect
    module.DB = project.database
    module.ENGINE = "MergeTree"
    module.STOP = str(project.root / "STOP")
    module.THROTTLE = ""
    return module, clients, trace


def _wait_for_writers(paths):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if all(path.exists() and '"t_ack"' in path.read_text() for path in paths):
            return
        time.sleep(0.05)
    raise TimeoutError("Original writers did not acknowledge their initial batches")


def _server_duplicate_report(project, trace, paths):
    intervals = _server_intervals(project, trace)
    start = intervals[trace["create_id"]][0]
    end = trace["snapshot_end"]
    duplicates = {
        row[0]
        for row in project.client.query(
            f"SELECT id FROM {project.database}.t GROUP BY id HAVING count() > 1"
        ).result_rows
    }
    attributed, outside = set(), set()
    for path in paths:
        for line in path.read_text().splitlines():
            batch = json.loads(line)
            if "t_ack" not in batch:
                continue
            ids = duplicates.intersection(range(batch["first"], batch["last"] + 1))
            if not ids:
                continue
            query_id = trace["queries"][(batch["kind"], batch["first"])]
            assert query_id in intervals, f"No server timing for {query_id}"
            began, finished = intervals[query_id]
            attributed.update(ids)
            if finished < start or began > end:
                outside.update(ids)
    return {
        "duplicate_ids_inside_window": len(attributed - outside),
        "duplicate_ids_outside_window": len(outside),
        "unattributed_duplicate_ids": len(duplicates - attributed),
        "server_window_start_us": start,
        "server_window_end_us": end,
    }


def _server_intervals(project, trace):
    project.client.command("SYSTEM FLUSH LOGS")
    rows = project.client.query(
        "SELECT query_id, toUnixTimestamp64Micro(query_start_time_microseconds), "
        "toUnixTimestamp64Micro(event_time_microseconds) FROM system.query_log "
        "WHERE type = 'QueryFinish' AND startsWith(query_id, {prefix:String})",
        parameters={"prefix": trace["prefix"]},
    ).result_rows
    intervals = {query_id: (start, end) for query_id, start, end in rows}
    flushes = project.client.query(
        "SELECT query_id, toUnixTimestamp64Micro(flush_time_microseconds) "
        "FROM system.asynchronous_insert_log WHERE database = {db:String} "
        "AND table = 't' AND status = 'Ok'",
        parameters={"db": project.database},
    ).result_rows
    for query_id, flushed in flushes:
        intervals[query_id] = (flushed, flushed)
    return intervals
