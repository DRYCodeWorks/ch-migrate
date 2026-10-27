"""Real-server waiting: `up` records a migration only after the work it started finishes."""

import os
import subprocess
import sys
import time
from contextlib import contextmanager

import pytest
import yaml

pytestmark = pytest.mark.integration

# Mutations ClickHouse starts and answers before finishing; STOP MERGES holds each one.
HELD = {
    "update": "ALTER TABLE {db}.counter UPDATE x = 1 WHERE id = 1",
    "delete": "ALTER TABLE {db}.counter DELETE WHERE id = 5",
    "materialize": "ALTER TABLE {db}.counter MATERIALIZE COLUMN doubled",
}


@pytest.mark.parametrize("statement", HELD.values(), ids=HELD.keys())
def test_up_records_a_migration_only_after_its_mutation(project, statement):
    _seed(project)
    _upgrade_sql(project, statement)
    project.stop_merges(f"{project.database}.counter")
    try:
        held = project.run("up", "it", "--timeout", "1")
        assert held.exit_code == 1 and "Timed out" in held.output, held.output
        assert "parts_to_do=" in held.output
        assert _heads(project) == ["aaaa"]
    finally:
        project.client.command(f"SYSTEM START MERGES {project.database}.counter")
    resumed = project.run("up", "it")
    assert resumed.exit_code == 0, resumed.output
    assert all(row[2] for row in _mutations(project))
    assert _heads(project) == ["bbbb"]


def test_failed_mutation_is_not_recorded_and_names_its_kill(project):
    _seed(project)
    _upgrade_sql(project, "ALTER TABLE {db}.counter UPDATE x = throwIf(x = 5) WHERE 1")
    result = project.run("up", "it")
    assert result.exit_code == 1, result.output
    failed = next(row for row in _mutations(project) if row[4])
    assert failed[0] in result.output and "throwIf" in result.output
    assert f"mutation_id = '{failed[0]}'" in result.output
    assert _heads(project) == ["aaaa"]


def test_killed_mutation_then_fixed_sql_runs_again(project):
    """DRY-1604: after the kill, fixing the file and running `up` is the whole recovery."""
    db = project.database
    project.client.command(
        f"CREATE TABLE {db}.orders (id UInt64, raw String, qty UInt64) ENGINE=MergeTree ORDER BY id"
    )
    project.client.command(f"INSERT INTO {db}.orders VALUES (1, '3', 0), (2, 'n/a', 0)")
    _baseline(project)
    path = _upgrade_sql(project, "ALTER TABLE {db}.orders UPDATE qty = toUInt64(raw) WHERE 1")
    failed = project.run("up", "it")
    assert failed.exit_code == 1 and "Cannot parse" in failed.output, failed.output
    blocked = project.run("up", "it")
    assert blocked.exit_code == 1, blocked.output
    assert "started before this run" in blocked.output and "KILL MUTATION" in blocked.output
    mutation = next(row[0] for row in _mutations(project, "orders") if row[4])
    project.client.command(
        "KILL MUTATION WHERE database = {db:String} AND mutation_id = {mutation:String}",
        parameters={"db": db, "mutation": mutation},
    )
    _until(lambda: not any(row[4] for row in _mutations(project, "orders")), "kill did not land")
    _write_sql(path, "ALTER TABLE {db}.orders UPDATE qty = toUInt64OrZero(raw) WHERE 1")
    fixed = project.run("up", "it")
    assert fixed.exit_code == 0, fixed.output
    rows = project.client.query(f"SELECT id, qty FROM {db}.orders ORDER BY id").result_rows
    assert rows == [(1, 3), (2, 0)]
    assert _heads(project) == ["bbbb"]


def test_rejected_statement_can_be_fixed_and_run_again(project):
    """DRY-1604: ClickHouse refuses a sorting-key change; the corrected file then applies."""
    _seed(project)
    path = _upgrade_sql(project, "ALTER TABLE {db}.counter MODIFY COLUMN id String")
    rejected = project.run("up", "it")
    assert (
        rejected.exit_code == 1 and "ALTER_OF_COLUMN_IS_FORBIDDEN" in rejected.output
    ), rejected.output
    assert _heads(project) == ["aaaa"]
    _write_sql(path, "ALTER TABLE {db}.counter UPDATE x = 1 WHERE id = 1")
    fixed = project.run("up", "it")
    assert fixed.exit_code == 0, fixed.output
    assert _heads(project) == ["bbbb"]


def test_unfinished_earlier_mutation_holds_the_run(project):
    """Work already queued on the database finishes before any migration statement is sent."""
    db = project.database
    _seed(project)
    project.client.command(f"CREATE TABLE {db}.observed (value UInt8) ENGINE=Memory")
    _upgrade_sql(project, "INSERT INTO {db}.observed VALUES (1)")
    project.stop_merges(f"{db}.counter")
    try:
        project.client.command(f"ALTER TABLE {db}.counter UPDATE x = 7 WHERE id = 5")
        held = project.run("up", "it", "--timeout", "1")
        assert held.exit_code == 1 and "Timed out" in held.output, held.output
        assert project.client.command(f"SELECT count() FROM {db}.observed") == 0
        assert _heads(project) == ["aaaa"]
    finally:
        project.client.command(f"SYSTEM START MERGES {db}.counter")
    resumed = project.run("up", "it")
    assert resumed.exit_code == 0, resumed.output
    assert project.client.command(f"SELECT count() FROM {db}.observed") == 1
    assert _heads(project) == ["bbbb"]


def test_down_waits_for_its_mutation(project):
    db = project.database
    _seed(project)
    project.write_revision(
        "bbbb",
        {
            "upgrade": 'op.execute("SELECT 1")',
            "downgrade": "# ch-migrate: allow-non-idempotent Controlled downgrade backfill\n"
            "op.execute(f'ALTER TABLE {db}.counter UPDATE x = 0 WHERE id = 5')",
        },
        "aaaa",
    )
    assert project.run("up", "it").exit_code == 0
    project.stop_merges(f"{db}.counter")
    try:
        with _running(project, ("down", "it")) as (process, log):
            _until(lambda: _mutations(project), "downgrade mutation was not submitted")
            time.sleep(1)
            assert process.poll() is None, log.read_text()
            project.client.command(f"SYSTEM START MERGES {db}.counter")
            assert process.wait(timeout=30) == 0, log.read_text()
    finally:
        project.client.command(f"SYSTEM START MERGES {db}.counter")
    assert all(row[2] for row in _mutations(project))
    assert _heads(project) == ["aaaa"]


def test_mutation_polling_preserves_session_settings(project):
    _seed(project)
    project.client.command(f"CREATE TABLE {project.database}.observed (value UInt64) ENGINE=Memory")
    config_path = project.root / "config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["environments"]["it"]["session_timeout"] = 1
    config_path.write_text(yaml.safe_dump(config))
    _upgrade_sql(
        project,
        "SET max_threads=3;\n"
        "-- ch-migrate: allow-non-idempotent increment once\n"
        "ALTER TABLE {db}.counter UPDATE x=x+1 WHERE id=1;\n"
        "-- ch-migrate: allow-non-idempotent record the preserved session setting\n"
        "INSERT INTO {db}.observed SELECT getSetting('max_threads')",
    )
    project.stop_merges(f"{project.database}.counter")
    try:
        with _running(project, ("up", "it")) as (process, log):
            mutation = _until(lambda: _mutations(project), "mutation was not submitted")[0][0]
            _until(lambda: mutation in log.read_text(), "wait was not reported")
            time.sleep(2.2)  # Longer than the server session timeout, while polling continues.
            assert process.poll() is None, log.read_text()
            project.client.command(f"SYSTEM START MERGES {project.database}.counter")
            assert process.wait(timeout=30) == 0, log.read_text()
        assert project.client.query(
            f"SELECT value FROM {project.database}.observed"
        ).result_rows == [(3,)]
    finally:
        project.client.command(f"SYSTEM START MERGES {project.database}.counter")


@pytest.mark.parametrize("mode", ["execute", "driver", "executemany"])
def test_mutation_raw_bind_paths_are_waited(project, mode):
    _seed(project)
    if mode == "execute":
        body = (
            "from sqlalchemy import text\n"
            "# ch-migrate: allow-non-idempotent bound counter update\n"
            "op.get_bind().execute(text(f'ALTER TABLE {db}.counter UPDATE x=x+:delta WHERE id=:key'), {'delta': 1, 'key': 1})"
        )
    else:
        values = (
            "[{'delta': 1, 'key': 1}, {'delta': 1, 'key': 5}]"
            if mode == "executemany"
            else "{'delta': 1, 'key': 1}"
        )
        body = (
            "# ch-migrate: allow-non-idempotent bound counter update\n"
            f"op.get_bind().exec_driver_sql(f'ALTER TABLE {{db}}.counter UPDATE x=x+%(delta)s WHERE id=%(key)s', {values})"
        )
    project.write_revision("bbbb", {"upgrade": body}, "aaaa")
    result = project.run("up", "it")
    assert result.exit_code == 0, result.output
    assert all(row[2] for row in _mutations(project))
    assert project.client.query(
        f"SELECT id, x FROM {project.database}.counter ORDER BY id"
    ).result_rows == [(1, 1), (5, 6 if mode == "executemany" else 5)]
    assert _heads(project) == ["bbbb"]


def test_mutation_piped_progress_prints_plain_lines(project):
    """CI logs and agents get a start line naming the mutation, never a redrawn bar."""
    _seed(project)
    _upgrade_sql(project, "ALTER TABLE {db}.counter UPDATE x=x+1 WHERE id=1")
    project.stop_merges(f"{project.database}.counter")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "ch_migrate.cli", "up", "it", "--timeout", "1"],
            cwd=project.root,
            env={**os.environ, "TERM": "xterm-256color"},
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 1, result.stderr
        start = f"  waiting on mutation_2.txt ({project.database}.counter): 0/1 parts"
        assert start in result.stderr.splitlines(), result.stderr
        assert "\x1b" not in result.stderr and "\r" not in result.stderr
        assert "Run ch-migrate up it again: it waits for that work to finish" in result.stderr
    finally:
        project.client.command(f"SYSTEM START MERGES {project.database}.counter")
    assert _heads(project) == ["aaaa"]


@pytest.mark.skipif(os.name != "posix", reason="A real PTY is required for terminal progress")
def test_mutation_terminal_progress_uses_a_live_line(project):
    import pty
    import select

    _seed(project)
    _upgrade_sql(project, "ALTER TABLE {db}.counter UPDATE x=x+1 WHERE id=1")
    project.stop_merges(f"{project.database}.counter")
    master, slave = pty.openpty()
    process = None
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "ch_migrate.cli", "up", "it", "--timeout", "0.8"],
            cwd=project.root,
            env={**os.environ, "TERM": "xterm-256color"},
            stdout=subprocess.DEVNULL,
            stderr=slave,
        )
        # Keep the PTY alive and drain it while the child writes; do not depend
        # on buffered output surviving the final slave close.
        chunks = []
        deadline = time.monotonic() + 30
        while process.poll() is None or select.select([master], [], [], 0)[0]:
            assert time.monotonic() < deadline, "terminal run did not finish"
            if select.select([master], [], [], 0.2)[0]:
                chunks.append(os.read(master, 4096))
        assert process.wait(timeout=5) == 1
        output = b"".join(chunks)
        # The live bar redraws in place; the timeout still names what was pending.
        assert "━".encode() in output and b" parts" in output
        assert b"Timed out" in output and b"parts_to_do=" in output
        assert _heads(project) == ["aaaa"]
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        if slave is not None:
            os.close(slave)
        os.close(master)
        project.client.command(f"SYSTEM START MERGES {project.database}.counter")


def test_mutation_lightweight_update_finishes_without_fabricating_a_mutation(project):
    project.client.command(
        f"CREATE TABLE {project.database}.patches (id UInt64, x UInt64) "
        "ENGINE=MergeTree ORDER BY id SETTINGS enable_block_number_column=1, "
        "enable_block_offset_column=1"
    )
    project.client.command(f"INSERT INTO {project.database}.patches VALUES (1, 0)")
    project.write_revision(
        "aaaa",
        {
            "upgrade": 'op.execute("SET allow_experimental_lightweight_update=1")\n'
            "# ch-migrate: allow-non-idempotent apply one synchronous patch\n"
            'op.get_bind().exec_driver_sql(f"UPDATE {db}.patches SET x=x+1 WHERE id=1")'
        },
    )
    result = project.run("up", "it")
    assert result.exit_code == 0, result.output
    assert project.client.command(f"SELECT x FROM {project.database}.patches") == 1
    assert _mutations(project, "patches") == []
    assert _heads(project) == ["aaaa"]


def test_mutation_waits_for_every_replica(cluster_project, clickhouse_cluster):
    project, cluster = cluster_project, clickhouse_cluster
    _seed_cluster(project, cluster)
    _upgrade_sql(project, "ALTER TABLE {db}.counter UPDATE x = x + 1 WHERE id = 1")
    table = f"{project.database}.counter"
    for action in ("MERGES", "FETCHES", "REPLICATION QUEUES"):
        cluster.clients[2].command(f"SYSTEM STOP {action} {table}")
    try:
        with _running(project, ("up", "it")) as (process, log):
            _until(lambda: _mutations(project), "replicated mutation was not submitted")
            cluster.clients[2].command(f"SYSTEM SYNC REPLICA {table} PULL")
            _until(lambda: all(row[2] for row in _mutations(project)), "initiator did not finish")
            second = (
                cluster.clients[2]
                .query(
                    "SELECT is_done FROM system.mutations WHERE database = {db:String} AND table = 'counter'",
                    parameters={"db": project.database},
                )
                .result_rows
            )
            assert second and all(not row[0] for row in second)
            _until(
                lambda: "waiting on mutation" in log.read_text(), "replica wait was not reported"
            )
            assert process.poll() is None
            for action in ("MERGES", "FETCHES", "REPLICATION QUEUES"):
                cluster.clients[2].command(f"SYSTEM START {action} {table}")
            assert process.wait(timeout=30) == 0, log.read_text()
        for client in cluster.clients.values():
            assert client.command(f"SELECT x FROM {table} WHERE id = 1") == 1
    finally:
        for action in ("MERGES", "FETCHES", "REPLICATION QUEUES"):
            cluster.clients[2].command(f"SYSTEM START {action} {table}")


def test_mutation_on_a_down_replica_is_not_success(cluster_project, clickhouse_cluster):
    project, cluster = cluster_project, clickhouse_cluster
    _seed_cluster(project, cluster)
    _upgrade_sql(project, "ALTER TABLE {db}.counter UPDATE x = 1 WHERE id = 1")
    table = f"{project.database}.counter"
    for client in cluster.clients.values():
        client.command(f"SYSTEM STOP MERGES {table}")
    try:
        first = project.run("up", "it", "--timeout", "0.5")
        assert first.exit_code == 1, first.output
        cluster.stop_node(2)
        result = project.run("up", "it", "--timeout", "1")
        assert result.exit_code == 1 and "Timed out" in result.output, result.output
        assert "shows 1/2 replicas" in result.output
        assert _heads(project) == ["aaaa"]
    finally:
        if 2 in cluster.stopped:
            cluster.start_node(2)
        for client in cluster.clients.values():
            client.command(f"SYSTEM START MERGES {table}")
    resumed = project.run("up", "it")
    assert resumed.exit_code == 0, resumed.output
    assert _heads(project) == ["bbbb"]


def _seed_cluster(project, cluster):
    table = f"{project.database}.counter"
    cluster.clients[1].command(
        f"CREATE TABLE {table} ON CLUSTER {cluster.name} (id UInt64, x UInt64) "
        f"ENGINE=ReplicatedMergeTree('/clickhouse/waiting/{project.database}/counter', '{{replica}}') ORDER BY id"
    )
    cluster.clients[1].command(f"INSERT INTO {table} VALUES (1, 0), (5, 5)")
    cluster.clients[2].command(f"SYSTEM SYNC REPLICA {table}")
    project.write_revision("aaaa", {"upgrade": 'op.execute("SELECT 1")'})
    result = project.run("up", "it")
    assert result.exit_code == 0, result.output


def _seed(project):
    project.client.command(
        f"CREATE TABLE {project.database}.counter (id UInt64, x UInt64, "
        "doubled UInt64 DEFAULT x * 2) ENGINE=MergeTree ORDER BY id"
    )
    project.client.command(f"INSERT INTO {project.database}.counter (id, x) VALUES (1, 0), (5, 5)")
    _baseline(project)


def _baseline(project):
    project.write_revision("aaaa", {"upgrade": 'op.execute("SELECT 1")'})
    result = project.run("up", "it")
    assert result.exit_code == 0, result.output


def _upgrade_sql(project, sql):
    path = project.sql_dir / "mutation.sql"
    _write_sql(path, sql)
    project.write_revision(
        "bbbb",
        {"upgrade": "from ch_migrate import run_sql\nrun_sql('mutation.sql')"},
        "aaaa",
    )
    return path


def _write_sql(path, sql):
    path.write_text(
        "-- ch-migrate: allow-non-idempotent Controlled waiting acceptance scenario\n" + sql + ";\n"
    )


def _mutations(project, table="counter"):
    return project.client.query(
        "SELECT mutation_id, command, is_done, parts_to_do, latest_fail_reason FROM system.mutations "
        "WHERE database = {db:String} AND table = {table:String} AND "
        + project.REWRITES_PARTS
        + " ORDER BY mutation_id",
        parameters={"db": project.database, "table": table},
    ).result_rows


def _heads(project):
    return [
        row[0]
        for row in project.client.query(
            f"SELECT version_num FROM {project.database}.alembic_version ORDER BY version_num"
        ).result_rows
    ]


def _until(predicate, message):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    pytest.fail(message)


@contextmanager
def _running(project, args, code=None):
    path = project.root / f"run-{time.monotonic_ns()}.log"
    command = [sys.executable, "-c", code] if code else [sys.executable, "-m", "ch_migrate.cli"]
    with path.open("w") as log:
        process = subprocess.Popen(
            [*command, *args],
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
