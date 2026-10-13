"""ClickHouse Cloud-only checks; they skip on self-hosted servers (DRY-1413)."""

import subprocess
import sys
import time

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


def test_cloud_up_advances_version_only_after_mutation_finishes(project):
    """Cloud cannot hold a mutation with STOP MERGES; a deliberately slow one shows the wait."""
    db = project.database
    _slow_increment(project, seconds=2)
    result = project.run("up", "it")
    assert result.exit_code == 0, result.output
    assert (
        project.client.command(
            f"SELECT x FROM {db}.counter", settings={"select_sequential_consistency": 1}
        )
        == 1
    )
    mutated, versioned = _part_log_times(project)
    print(f"\nCLOUD_WAIT mutation_done={mutated} version_row={versioned}")
    assert mutated.year > 1970, "part_log shows no MutatePart for counter; waiting unproven"
    assert versioned >= mutated


def test_cloud_killed_up_reattaches_to_its_running_mutation(project):
    """SIGKILL mid-wait; the rerun must resume from its journal, never resubmit the UPDATE."""
    db = project.database
    _slow_increment(project, seconds=3)
    log_path = project.root / "first-up.log"
    with log_path.open("w") as log:
        first = subprocess.Popen(
            [sys.executable, "-m", "ch_migrate.cli", "up", "it"],
            cwd=project.root,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            _until_running_mutation(project)
        finally:
            first.kill()
            first.wait(timeout=10)
    resumed = project.run("up", "it")
    assert resumed.exit_code == 0, resumed.output
    assert "not reissuing SQL" in resumed.output, resumed.output
    increments = project.client.command(
        f"SELECT x FROM {db}.counter", settings={"select_sequential_consistency": 1}
    )
    rewrites = project.client.command(
        "SELECT uniqExact(mutation_id) FROM system.mutations "
        "WHERE database = {db:String} AND table = {table:String} AND " + project.REWRITES_PARTS,
        parameters={"db": db, "table": "counter"},
    )
    print(f"\nCLOUD_SIGKILL increments={increments} owned_mutations={rewrites}")
    assert (increments, rewrites) == (1, 1)
    assert project.client.query(f"SELECT version_num FROM {db}.alembic_version").result_rows == [
        ("aaaa",)
    ]


def _slow_increment(project, seconds):
    db = project.database
    project.client.command(
        f"CREATE TABLE {db}.counter (id UInt64, x UInt64) ENGINE = MergeTree ORDER BY id"
    )
    project.client.command(f"INSERT INTO {db}.counter VALUES (1, 0)")
    (project.sql_dir / "slow.sql").write_text(
        "-- ch-migrate: allow-non-idempotent One controlled increment\n"
        f"ALTER TABLE {{db}}.counter UPDATE x = x + 1 + sleepEachRow({seconds}) WHERE id = 1;\n"
    )
    project.write_revision(
        "aaaa", {"upgrade": "from ch_migrate import run_sql\nrun_sql('slow.sql')"}
    )


def _until_running_mutation(project):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        pending = project.client.command(
            "SELECT count() FROM system.mutations WHERE database = {db:String} "
            "AND table = 'counter' AND NOT is_done",
            parameters={"db": project.database},
        )
        if pending:
            return
        time.sleep(0.05)
    pytest.fail("The slow UPDATE was never seen running")


def _part_log_times(project):
    """Latest mutated `counter` part and the version row's part, across every replica."""
    deadline = time.monotonic() + 60
    while True:
        project.client.command("SYSTEM FLUSH LOGS")
        mutated, versioned = project.client.query(
            "SELECT maxIf(event_time_microseconds, table = 'counter' AND event_type = 'MutatePart'), "
            "maxIf(event_time_microseconds, table = 'alembic_version' AND event_type = 'NewPart') "
            "FROM clusterAllReplicas('default', merge('system', '^part_log')) "
            "WHERE database = {db:String}",
            parameters={"db": project.database},
            settings={"skip_unavailable_shards": 1},
        ).result_rows[0]
        if (mutated.year > 1970 and versioned.year > 1970) or time.monotonic() > deadline:
            return mutated, versioned
        time.sleep(2)
