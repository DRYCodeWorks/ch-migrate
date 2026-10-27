"""Rerunning an interrupted step: which steps ch-migrate vouches for, and the marker table."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from ch_migrate.waiting.reruns import InterruptedStep, RerunGuard, Step, unvouched_statements


def test_a_first_run_records_the_step_before_it_runs(tmp_path):
    client = _Client(started=0)
    _guard(client).begin(_step(tmp_path, "op.execute('SELECT 1')"))
    assert any(sql.startswith("INSERT INTO `db`.`_ch_migrate_in_progress`") for sql in client.sent)


def test_an_interrupted_step_the_gate_passes_runs_again(tmp_path, caplog):
    client = _Client(started=1)
    step = _step(tmp_path, "op.execute('ALTER TABLE t ADD COLUMN IF NOT EXISTS c UInt8')")
    _guard(client).begin(step)
    assert "was interrupted on an earlier run; running it again" in caplog.text
    assert any(sql.startswith("INSERT INTO") for sql in client.sent)


def test_an_interrupted_step_with_a_waiver_stops(tmp_path):
    client = _Client(started=1)
    step = _step(
        tmp_path,
        "# ch-migrate: allow-non-idempotent counted once per deploy\n"
        "    op.execute('ALTER TABLE t UPDATE n = n + 1 WHERE 1')",
    )
    with pytest.raises(InterruptedStep) as stopped:
        _guard(client).begin(step)
    assert [item.why for item in stopped.value.unvouched] == ["waiver: counted once per deploy"]
    assert "--rerun-interrupted bbbbbbbb" in str(stopped.value)
    assert not any(sql.startswith("INSERT INTO") for sql in client.sent)


def test_naming_the_revision_lets_an_interrupted_step_run_again(tmp_path):
    client = _Client(started=1)
    step = _step(tmp_path, "op.execute(statement)")
    _guard(client, acknowledged={"bbbbbbbb"}).begin(step)
    assert any(sql.startswith("INSERT INTO") for sql in client.sent)


def test_a_recorded_step_clears_every_marker_for_its_revision(tmp_path):
    client = _Client(started=0)
    _guard(client).finish(_step(tmp_path, "op.execute('SELECT 1')"))
    assert client.sent == [
        "ALTER TABLE `db`.`_ch_migrate_in_progress` DELETE WHERE revision = "
        "'bbbbbbbbbbbb' SETTINGS mutations_sync = 2"
    ]


def test_downgrades_are_judged_by_the_same_rules(tmp_path):
    path = _revision(
        tmp_path,
        "op.execute('CREATE TABLE IF NOT EXISTS t (x UInt8) ENGINE = Memory')",
        downgrade="op.execute('INSERT INTO t SELECT 1')",
    )
    assert unvouched_statements(path, "upgrade") == []
    assert [item.why for item in unvouched_statements(path, "downgrade")] == [
        "not safe to run twice"
    ]


@pytest.mark.parametrize(
    ("engine", "cluster", "expected"),
    [
        ("Atomic", None, "ENGINE = MergeTree ORDER BY"),
        ("Shared", None, "ENGINE = MergeTree ORDER BY"),
        ("Replicated", None, "ENGINE = ReplicatedMergeTree ORDER BY"),
        (
            "Atomic",
            "main",
            "ON CLUSTER `main` (revision String, direction LowCardinality(String), started "
            "DateTime64(3) DEFAULT now64(3)) ENGINE = ReplicatedMergeTree("
            "'/clickhouse/ch_migrate/db/{shard}/_ch_migrate_in_progress', '{replica}')",
        ),
    ],
)
def test_the_marker_table_follows_the_deployment(engine, cluster, expected):
    state = _state(engine, cluster)
    assert expected in RerunGuard(_Client(started=0), state, frozenset())._create_sql()


class _Client:
    def __init__(self, started: int):
        self.started, self.sent = started, []

    def command(self, sql, parameters=None, settings=None):
        if sql.startswith("EXISTS TABLE"):
            return 1
        if sql.startswith("SELECT count()"):
            return self.started
        self.sent.append(sql)
        return None


def _guard(client, acknowledged=()):
    return RerunGuard(client, _state("Atomic", None), frozenset(acknowledged))


def _state(engine, cluster):
    on_cluster = cluster if engine == "Atomic" else None
    return SimpleNamespace(
        database="db", database_engine=engine, cluster=cluster, on_cluster=on_cluster
    )


def _step(tmp_path: Path, upgrade: str) -> Step:
    return Step("bbbbbbbbbbbb", "upgrade", _revision(tmp_path, upgrade))


def _revision(tmp_path: Path, upgrade: str, downgrade: str = "pass") -> Path:
    versions = tmp_path / "migrations" / "versions"
    versions.mkdir(parents=True, exist_ok=True)
    path = versions / "bbbb.py"
    path.write_text(
        "revision = 'bbbbbbbbbbbb'\ndown_revision = None\n\n"
        f"def upgrade():\n    {upgrade}\n\ndef downgrade():\n    {downgrade}\n"
    )
    return path
