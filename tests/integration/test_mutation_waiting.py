"""Real-server waiting, crash recovery, failure, and timeout invariants."""

import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.integration
CORPUS = yaml.safe_load((Path(__file__).parents[1] / "corpus/classification.yaml").read_text())
MUTATIONS = [case for case in CORPUS["statements"] if case["expected"] == "mutation"]


@pytest.mark.parametrize("case", MUTATIONS, ids=lambda case: case["id"])
def test_mutation_waits_for_server_corpus(project, case):
    values = {"db": project.database, "table": f"{project.database}.sample"}
    project.client.command(CORPUS["table_ddl"].format(**values))
    project.client.command(CORPUS["seed"].format(**values))
    if case["id"] == "lightweight_delete":
        # Lightweight deletes reject projections under the server's default policy.
        project.client.command(
            f"ALTER TABLE {values['table']} DROP PROJECTION pr SETTINGS mutations_sync=2"
        )
    sql = case["sql"].format(**values)
    project.write_revision(
        "aaaa",
        {
            "upgrade": "# ch-migrate: allow-non-idempotent Server-backed mutation corpus\n"
            + f"op.execute({sql!r})"
        },
    )
    result = project.run("up", "it")
    assert result.exit_code == 0, result.output
    rows = _mutations(project, "sample")
    assert all(row[2] for row in rows), rows
    assert any("chm_mutation_" in row[1] for row in rows), rows
    assert _heads(project) == ["aaaa"]


def test_mutation_failure_leaves_head_and_prints_operator_kill(project):
    _seed(project)
    _upgrade_sql(project, "ALTER TABLE {db}.counter UPDATE x = throwIf(x = 5) WHERE 1")
    result = project.run("up", "it")
    assert result.exit_code == 1, result.output
    rows = _mutations(project)
    failed = next(row for row in rows if row[4])
    assert failed[0] in result.output and "throwIf" in result.output
    assert "KILL MUTATION WHERE database =" in result.output
    assert f"mutation_id = '{failed[0]}'" in result.output
    assert _heads(project) == ["aaaa"]
    assert any(not row[2] and row[0] == failed[0] for row in _mutations(project))


def test_mutation_timeout_preserves_work_and_resumes(project):
    _seed(project)
    _upgrade_sql(project, "ALTER TABLE {db}.counter UPDATE x = x + 1 WHERE id = 1")
    project.client.command(f"SYSTEM STOP MERGES {project.database}.counter")
    try:
        result = project.run("up", "it", "--timeout", "0.5")
        assert result.exit_code == 1, result.output
        rows = _mutations(project)
        ids = {row[0] for row in rows}
        assert "Timed out" in result.output and "parts_to_do=" in result.output
        assert all(mutation in result.output for mutation in ids)
        assert _heads(project) == ["aaaa"]
        assert any(not row[2] for row in rows)
    finally:
        project.client.command(f"SYSTEM START MERGES {project.database}.counter")
    resumed = project.run("up", "it")
    assert resumed.exit_code == 0, resumed.output
    assert {row[0] for row in _mutations(project)} == ids
    assert project.client.command(f"SELECT x FROM {project.database}.counter WHERE id = 1") == 1
    assert _heads(project) == ["bbbb"]


@pytest.mark.parametrize("trial", range(3))
def test_mutation_sigkill_rerun_attaches_without_second_increment(project, trial):
    _seed(project)
    _upgrade_sql(project, "ALTER TABLE {db}.counter UPDATE x = x + 1 WHERE id = 1")
    project.client.command(f"SYSTEM STOP MERGES {project.database}.counter")
    try:
        with _running(project, ("up", "it")) as (first, first_log):
            rows = _until(lambda: _mutations(project), "mutation was not submitted")
            mutation = rows[0][0]
            _until(
                lambda: mutation in first_log.read_text(), "first process did not report waiting"
            )
            first.kill()
            first.wait(timeout=10)
        original = {row[0] for row in _mutations(project)}
        assert _heads(project) == ["aaaa"]
        with _running(project, ("up", "it")) as (resumed, log):
            _until(
                lambda: mutation in log.read_text(), "rerun did not report the original mutation"
            )
            assert resumed.poll() is None
            assert {row[0] for row in _mutations(project)} == original
            project.client.command(f"SYSTEM START MERGES {project.database}.counter")
            assert resumed.wait(timeout=30) == 0, log.read_text()
        assert project.client.command(f"SELECT x FROM {project.database}.counter WHERE id = 1") == 1
        assert _heads(project) == ["bbbb"]
    finally:
        project.client.command(f"SYSTEM START MERGES {project.database}.counter")


def test_mutation_crash_before_durable_receipt_recovers_from_intent(project):
    _seed(project)
    _upgrade_sql(project, "ALTER TABLE {db}.counter UPDATE x = x + 1 WHERE id = 1")
    # Fault injection kills the real CLI after server acceptance, before journal acknowledgement.
    code = """import os, signal
from ch_migrate.waiting import MigrationWaiter
original = MigrationWaiter._after
def crash(self, connection, cursor, statement, parameters, context, executemany):
    pending = self._pending.get(id(context))
    if pending and pending[1]['kind'] == 'mutation' and pending[1]['table'][1] == 'counter':
        os.kill(os.getpid(), signal.SIGKILL)
    return original(self, connection, cursor, statement, parameters, context, executemany)
MigrationWaiter._after = crash
from ch_migrate.cli import main
main()
"""
    project.client.command(f"SYSTEM STOP MERGES {project.database}.counter")
    try:
        with _running(project, ("up", "it"), code) as (crashed, log):
            assert crashed.wait(timeout=30) == -9, log.read_text()
        rows = _mutations(project)
        original_ids = {row[0] for row in rows}
        assert rows and any("chm_mutation_" in row[1] for row in rows)
        payload = project.client.query(
            f"SELECT argMax(payload, sequence) FROM {project.database}._ch_migrate_journal "
            "WHERE revision = 'bbbb' AND position = 1"
        ).result_rows[0][0]
        assert json.loads(payload)["phase"] == "intent"
        project.client.command(f"SYSTEM START MERGES {project.database}.counter")
        resumed = project.run("up", "it")
        assert resumed.exit_code == 0, resumed.output
        assert {row[0] for row in _mutations(project)} == original_ids
        assert project.client.command(f"SELECT x FROM {project.database}.counter WHERE id = 1") == 1
    finally:
        project.client.command(f"SYSTEM START MERGES {project.database}.counter")


def test_mutation_foreign_failure_is_not_adopted_or_followed(project):
    _seed(project)
    project.client.command(
        f"ALTER TABLE {project.database}.counter UPDATE x = throwIf(x = 5) WHERE 1"
    )
    _until(lambda: any(row[4] for row in _mutations(project)), "foreign mutation did not fail")
    original_ids = {row[0] for row in _mutations(project)}
    _upgrade_sql(project, "ALTER TABLE {db}.counter UPDATE x = x + 1 WHERE id = 1")
    result = project.run("up", "it")
    assert result.exit_code == 1 and "foreign mutation" in result.output, result.output
    assert {row[0] for row in _mutations(project)} == original_ids
    assert _heads(project) == ["aaaa"]


def test_mutation_expired_ownership_refuses_instead_of_replaying(project):
    _seed(
        project,
        " SETTINGS finished_mutations_to_keep=1, old_parts_lifetime=0, cleanup_delay_period=1, cleanup_delay_period_random_add=0",
    )
    _upgrade_sql(project, "ALTER TABLE {db}.counter UPDATE x = x + 1 WHERE id = 1")
    project.client.command(f"SYSTEM STOP MERGES {project.database}.counter")
    try:
        timed_out = project.run("up", "it", "--timeout", "0.4")
        assert timed_out.exit_code == 1
    finally:
        project.client.command(f"SYSTEM START MERGES {project.database}.counter")
    _until(lambda: all(row[2] for row in _mutations(project)), "owned mutation did not finish")
    project.client.command(
        f"ALTER TABLE {project.database}.counter DELETE WHERE 0 SETTINGS mutations_sync=2"
    )
    _until(
        lambda: not any("chm_mutation_" in row[1] for row in _mutations(project)),
        "ownership did not expire",
    )
    resumed = project.run("up", "it")
    assert resumed.exit_code == 1 and "Outcome unknown" in resumed.output, resumed.output
    assert "not reissued" in resumed.output
    assert project.client.command(f"SELECT x FROM {project.database}.counter WHERE id = 1") == 1
    assert _heads(project) == ["aaaa"]


def test_mutation_changed_sql_refuses_replay(project):
    _seed(project)
    path = _upgrade_sql(project, "ALTER TABLE {db}.counter UPDATE x = x + 1 WHERE id = 1")
    project.client.command(f"SYSTEM STOP MERGES {project.database}.counter")
    try:
        assert project.run("up", "it", "--timeout", "0.4").exit_code == 1
        before = {row[0] for row in _mutations(project)}
        path.write_text(path.read_text().replace("x + 1", "x + 2"))
        result = project.run("up", "it")
        assert result.exit_code == 1 and "changed" in result.output, result.output
        assert {row[0] for row in _mutations(project)} == before
        assert _heads(project) == ["aaaa"]
    finally:
        project.client.command(f"SYSTEM START MERGES {project.database}.counter")


def test_mutation_completed_prefix_is_not_repeated_and_downgrade_resets(project):
    _seed(project)
    project.client.command(f"CREATE TABLE {project.database}.audit (n UInt64) ENGINE=Memory")
    _upgrade_sql(
        project,
        "INSERT INTO {db}.audit VALUES (1);\n-- ch-migrate: allow-non-idempotent increment once\nALTER TABLE {db}.counter UPDATE x = x + 1 WHERE id = 1",
    )
    project.client.command(f"SYSTEM STOP MERGES {project.database}.counter")
    try:
        assert project.run("up", "it", "--timeout", "0.4").exit_code == 1
    finally:
        project.client.command(f"SYSTEM START MERGES {project.database}.counter")
    resumed = project.run("up", "it")
    assert resumed.exit_code == 0, resumed.output
    assert project.client.command(f"SELECT count() FROM {project.database}.audit") == 1
    reverted = project.run("down", "it")
    assert reverted.exit_code == 0, reverted.output
    reapplied = project.run("up", "it")
    assert reapplied.exit_code == 0, reapplied.output
    assert project.client.command(f"SELECT count() FROM {project.database}.audit") == 2
    assert project.client.command(f"SELECT x FROM {project.database}.counter WHERE id = 1") == 2


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
            _until(lambda: "parts_to_do=" in log.read_text(), "replica wait was not reported")
            assert process.poll() is None
            for action in ("MERGES", "FETCHES", "REPLICATION QUEUES"):
                cluster.clients[2].command(f"SYSTEM START {action} {table}")
            assert process.wait(timeout=30) == 0, log.read_text()
        for client in cluster.clients.values():
            assert client.command(f"SELECT x FROM {table} WHERE id = 1") == 1
    finally:
        for action in ("MERGES", "FETCHES", "REPLICATION QUEUES"):
            cluster.clients[2].command(f"SYSTEM START {action} {table}")


def test_mutation_missing_replica_is_not_success(cluster_project, clickhouse_cluster):
    project, cluster = cluster_project, clickhouse_cluster
    _seed_cluster(project, cluster)
    _upgrade_sql(project, "ALTER TABLE {db}.counter UPDATE x = x + 1 WHERE id = 1")
    table = f"{project.database}.counter"
    for client in cluster.clients.values():
        client.command(f"SYSTEM STOP MERGES {table}")
    try:
        first = project.run("up", "it", "--timeout", "0.5")
        assert first.exit_code == 1, first.output
        original = {row[0] for row in _mutations(project)}
        cluster.stop_node(2)
        result = project.run("up", "it", "--timeout", "1")
        assert result.exit_code == 1 and "Timed out" in result.output, result.output
        assert "host" in result.output
        assert _heads(project) == ["aaaa"]
        assert {row[0] for row in _mutations(project)} == original
    finally:
        if 2 in cluster.stopped:
            cluster.start_node(2)
        for client in cluster.clients.values():
            client.command(f"SYSTEM START MERGES {table}")
    resumed = project.run("up", "it")
    assert resumed.exit_code == 0, resumed.output
    assert _heads(project) == ["bbbb"]


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
    project.client.command(f"SYSTEM STOP MERGES {project.database}.counter")
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


def test_mutation_replaced_table_refuses_resume(project):
    _seed(project)
    _upgrade_sql(project, "ALTER TABLE {db}.counter UPDATE x=x+1 WHERE id=1")
    project.client.command(f"SYSTEM STOP MERGES {project.database}.counter")
    result = project.run("up", "it", "--timeout", "0.4")
    assert result.exit_code == 1
    project.client.command(f"DROP TABLE {project.database}.counter SYNC")
    project.client.command(
        f"CREATE TABLE {project.database}.counter (id UInt64, x UInt64) ENGINE=MergeTree ORDER BY id"
    )
    project.client.command(f"INSERT INTO {project.database}.counter VALUES (1, 0)")
    resumed = project.run("up", "it")
    assert resumed.exit_code == 1 and "replaced" in resumed.output, resumed.output
    assert project.client.command(f"SELECT x FROM {project.database}.counter") == 0
    assert _heads(project) == ["aaaa"]


def test_mutation_healthy_foreign_predecessor_is_waited_before_submission(project):
    _seed(project)
    _upgrade_sql(project, "ALTER TABLE {db}.counter UPDATE x=x+1 WHERE id=1")
    project.client.command(f"SYSTEM STOP MERGES {project.database}.counter")
    project.client.command(f"ALTER TABLE {project.database}.counter UPDATE x=x+10 WHERE id=1")
    original = {row[0] for row in _mutations(project)}
    try:
        blocked = project.run("up", "it", "--timeout", "0.4")
        assert blocked.exit_code == 1, blocked.output
        assert {row[0] for row in _mutations(project)} == original
        assert all("chm_mutation_" not in row[1] for row in _mutations(project))
        assert _heads(project) == ["aaaa"]
    finally:
        project.client.command(f"SYSTEM START MERGES {project.database}.counter")
    resumed = project.run("up", "it")
    assert resumed.exit_code == 0, resumed.output
    assert project.client.command(f"SELECT x FROM {project.database}.counter WHERE id=1") == 11


@pytest.mark.parametrize("scope", ["session", "query"])
def test_mutation_ttl_materialization_setting_is_respected(project, scope):
    project.client.command(
        f"CREATE TABLE {project.database}.expiry (ts DateTime) ENGINE=MergeTree ORDER BY ts"
    )
    project.client.command(f"INSERT INTO {project.database}.expiry VALUES ('2020-01-01 00:00:00')")
    statement = "ALTER TABLE {db}.expiry MODIFY TTL ts + INTERVAL 100 YEAR"
    sql = (
        "SET materialize_ttl_after_modify=0;\n" + statement
        if scope == "session"
        else statement + " SETTINGS materialize_ttl_after_modify=0"
    )
    project.write_revision(
        "aaaa", {"upgrade": "from ch_migrate import run_sql\nrun_sql('ttl.sql')"}
    )
    (project.sql_dir / "ttl.sql").write_text(sql)
    result = project.run("up", "it")
    assert result.exit_code == 0, result.output
    assert _mutations(project, "expiry") == []
    assert "TTL" in project.client.command(f"SHOW CREATE TABLE {project.database}.expiry")


def test_mutation_timeout_budget_is_not_reset_for_next_statement(project):
    _seed(project)
    project.client.command(
        f"CREATE TABLE {project.database}.second (id UInt64, x UInt64) ENGINE=MergeTree ORDER BY id"
    )
    project.client.command(f"INSERT INTO {project.database}.second VALUES (1, 0)")
    _upgrade_sql(
        project,
        "ALTER TABLE {db}.counter UPDATE x=x+1 WHERE id=1;\n"
        "-- ch-migrate: allow-non-idempotent second controlled update\n"
        "ALTER TABLE {db}.second UPDATE x=x+1 WHERE id=1",
    )
    project.client.command(f"SYSTEM STOP MERGES {project.database}.counter")
    project.client.command(f"SYSTEM STOP MERGES {project.database}.second")
    try:
        with _running(project, ("up", "it", "--timeout", "1.5")) as (process, log):
            mutation = _until(lambda: _mutations(project), "first statement did not start")[0][0]
            _until(lambda: mutation in log.read_text(), "first wait was not visible")
            time.sleep(0.7)
            project.client.command(f"SYSTEM START MERGES {project.database}.counter")
            _until(lambda: _mutations(project, "second"), "second statement did not start")
            began_second = time.monotonic()
            assert process.wait(timeout=10) == 1, log.read_text()
            assert time.monotonic() - began_second < 1.3
            assert "second" in log.read_text() and "Timed out" in log.read_text()
        assert _heads(project) == ["aaaa"]
    finally:
        project.client.command(f"SYSTEM START MERGES {project.database}.counter")
        project.client.command(f"SYSTEM START MERGES {project.database}.second")


def test_mutation_version_rows_ignore_fire_and_forget_insert_settings(project):
    project.write_revision(
        "aaaa",
        {
            "upgrade": 'op.execute("SET async_insert=1, wait_for_async_insert=0, async_insert_use_adaptive_busy_timeout=0, async_insert_busy_timeout_ms=60000")'
        },
    )
    project.write_revision("bbbb", {"upgrade": 'op.execute("SELECT 1")'}, "aaaa")
    result = project.run("up", "it")
    assert result.exit_code == 0, result.output
    assert _heads(project) == ["bbbb"]
    queued = project.client.query(
        "SELECT count() FROM system.asynchronous_inserts WHERE database = {db:String} "
        "AND table IN ('alembic_version', '_ch_migrate_journal')",
        parameters={"db": project.database},
    ).result_rows[0][0]
    assert queued == 0


def test_mutation_version_insert_lost_receipt_finishes_bookkeeping_only(project):
    _seed(project)
    project.client.command(f"CREATE TABLE {project.database}.audit (n UInt64) ENGINE=Memory")
    _upgrade_sql(project, "INSERT INTO {db}.audit VALUES (1)")
    code = """import os, signal
from ch_migrate.waiting_versions import VersionWrites
original = VersionWrites._insert
def crash(self, step, checkpoint):
    original(self, step, checkpoint)
    if step['value'] == 'bbbb':
        os.kill(os.getpid(), signal.SIGKILL)
VersionWrites._insert = crash
from ch_migrate.cli import main
main()
"""
    with _running(project, ("up", "it"), code) as (process, log):
        assert process.wait(timeout=30) == -9, log.read_text()
    assert _heads(project) == ["aaaa", "bbbb"]
    resumed = project.run("up", "it")
    assert resumed.exit_code == 0, resumed.output
    assert _heads(project) == ["bbbb"]
    assert project.client.command(f"SELECT count() FROM {project.database}.audit") == 1


def test_mutation_transport_loss_is_not_treated_as_server_rejection(project):
    _seed(project)
    _upgrade_sql(project, "CREATE TABLE IF NOT EXISTS {db}.uncertain (id UInt64) ENGINE=Memory")
    code = """from ch_migrate.waiting import MigrationWaiter
from clickhouse_connect.driver.exceptions import OperationalError
original = MigrationWaiter._after
def lose_reply(self, connection, cursor, statement, parameters, context, executemany):
    pending = self._pending.get(id(context))
    if pending and pending[1].get('table', [None, None])[-1] == 'uncertain':
        raise OperationalError('Connection lost after server accepted the statement')
    return original(self, connection, cursor, statement, parameters, context, executemany)
MigrationWaiter._after = lose_reply
from ch_migrate.cli import main
main()
"""
    with _running(project, ("up", "it"), code) as (process, log):
        assert process.wait(timeout=30) == 1, log.read_text()
    assert project.client.command(f"EXISTS TABLE {project.database}.uncertain") == 1
    resumed = project.run("up", "it")
    assert resumed.exit_code == 1 and "Outcome unknown" in resumed.output, resumed.output
    assert "Read-only inspection:" in resumed.output
    assert _heads(project) == ["aaaa"]
    project.client.command("SYSTEM FLUSH LOGS")
    requests = project.client.query(
        "SELECT count() FROM system.query_log WHERE type = 'QueryStart' "
        "AND startsWith(query, {prefix:String})",
        parameters={"prefix": f"CREATE TABLE IF NOT EXISTS {project.database}.uncertain"},
    ).result_rows[0][0]
    assert requests == 1


def test_mutation_trailing_foreign_failure_does_not_hold_owned_completion(project):
    _seed(project)
    _upgrade_sql(project, "ALTER TABLE {db}.counter UPDATE x=x+1 WHERE id=1")
    code = """from ch_migrate.waiting import MigrationWaiter
original = MigrationWaiter._after
def append_foreign(self, connection, cursor, statement, parameters, context, executemany):
    pending = self._pending.get(id(context))
    if pending and pending[1]['kind'] == 'mutation' and pending[1]['table'][1] == 'counter':
        self.client.command(f"ALTER TABLE {self.state.database}.counter UPDATE x=throwIf(x=5) WHERE 1")
    return original(self, connection, cursor, statement, parameters, context, executemany)
MigrationWaiter._after = append_foreign
from ch_migrate.cli import main
main()
"""
    with _running(project, ("up", "it"), code) as (process, log):
        assert process.wait(timeout=30) == 0, log.read_text()
    _until(lambda: any(row[4] for row in _mutations(project)), "trailing mutation did not fail")
    assert _heads(project) == ["bbbb"]
    assert project.client.command(f"SELECT x FROM {project.database}.counter WHERE id=1") == 1


def test_mutation_ttl_resume_does_not_materialize_twice(project):
    project.client.command(
        f"CREATE TABLE {project.database}.expiry (ts DateTime) ENGINE=MergeTree ORDER BY ts"
    )
    project.client.command(f"INSERT INTO {project.database}.expiry VALUES ('2020-01-01 00:00:00')")
    project.write_revision("aaaa", {"upgrade": 'op.execute("SELECT 1")'})
    assert project.run("up", "it").exit_code == 0
    _upgrade_sql(project, "ALTER TABLE {db}.expiry MODIFY TTL ts + INTERVAL 100 YEAR")
    project.client.command(f"SYSTEM STOP MERGES {project.database}.expiry")
    try:
        timed_out = project.run("up", "it", "--timeout", "0.4")
        assert timed_out.exit_code == 1, timed_out.output
        original = {row[0] for row in _mutations(project, "expiry")}
        assert any(not row[2] for row in _mutations(project, "expiry"))
    finally:
        project.client.command(f"SYSTEM START MERGES {project.database}.expiry")
    resumed = project.run("up", "it")
    assert resumed.exit_code == 0, resumed.output
    assert {row[0] for row in _mutations(project, "expiry")} == original
    assert _heads(project) == ["bbbb"]


@pytest.mark.skipif(os.name != "posix", reason="A real PTY is required for terminal progress")
def test_mutation_terminal_progress_uses_a_live_line(project):
    import pty
    import select

    _seed(project)
    _upgrade_sql(project, "ALTER TABLE {db}.counter UPDATE x=x+1 WHERE id=1")
    project.client.command(f"SYSTEM STOP MERGES {project.database}.counter")
    master, slave = pty.openpty()
    process = None
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "ch_migrate.cli", "up", "it", "--timeout", "0.8"],
            cwd=project.root,
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
        assert b"\x1b[K" in output and b"parts_to_do=" in output
        assert _heads(project) == ["aaaa"]
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        if slave is not None:
            os.close(slave)
        os.close(master)
        project.client.command(f"SYSTEM START MERGES {project.database}.counter")


def test_mutation_parameterized_ttl_batch_preserves_each_operation(project):
    project.client.command(
        f"CREATE TABLE {project.database}.expiry (ts DateTime) ENGINE=MergeTree ORDER BY ts"
    )
    project.client.command(f"INSERT INTO {project.database}.expiry VALUES ('2020-01-01 00:00:00')")
    project.write_revision(
        "aaaa",
        {
            "upgrade": "op.get_bind().exec_driver_sql("
            "f'ALTER TABLE {db}.expiry MODIFY TTL ts + toIntervalYear(%(years)s)', "
            "[{'years': 100}, {'years': 99}])"
        },
    )
    result = project.run("up", "it")
    assert result.exit_code == 0, result.output
    rows = _mutations(project, "expiry")
    assert all(row[2] for row in rows)
    assert len({row[0] for row in rows if "chm_mutation_" in row[1]}) == 2
    assert _heads(project) == ["aaaa"]


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


def _seed(project, settings=""):
    project.client.command(
        f"CREATE TABLE {project.database}.counter (id UInt64, x UInt64) ENGINE=MergeTree ORDER BY id{settings}"
    )
    project.client.command(f"INSERT INTO {project.database}.counter VALUES (1, 0), (5, 5)")
    project.write_revision("aaaa", {"upgrade": 'op.execute("SELECT 1")'})
    result = project.run("up", "it")
    assert result.exit_code == 0, result.output


def _upgrade_sql(project, sql):
    path = project.sql_dir / "mutation.sql"
    path.write_text(
        "-- ch-migrate: allow-non-idempotent Controlled waiting acceptance scenario\n" + sql + ";\n"
    )
    project.write_revision(
        "bbbb",
        {"upgrade": "from ch_migrate import run_sql\nrun_sql('mutation.sql')"},
        "aaaa",
    )
    return path


def _mutations(project, table="counter"):
    return project.client.query(
        "SELECT mutation_id, command, is_done, parts_to_do, latest_fail_reason FROM system.mutations "
        "WHERE database = {db:String} AND table = {table:String} ORDER BY mutation_id",
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
