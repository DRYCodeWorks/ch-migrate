"""Waiting decisions without a server: the deadline, what counts as a write, and completion."""

from types import SimpleNamespace

import pytest

from ch_migrate.waiting.budget import WaitBudget
from ch_migrate.waiting.display import WaitDisplay
from ch_migrate.waiting.errors import MutationFailed, WaitTimeout
from ch_migrate.waiting.mutations import MutationWaiter
from ch_migrate.waiting.sql import is_write


def test_deadline_is_shared_across_waits(monkeypatch):
    now = [10.0]
    monkeypatch.setattr("ch_migrate.waiting.budget.time.monotonic", lambda: now[0])
    budget = WaitBudget(timeout=2, started=now[0])
    now[0] = 11.5
    assert budget.elapsed == 1.5
    now[0] = 12.0
    with pytest.raises(WaitTimeout, match="second mutation"):
        budget.check("second mutation")


@pytest.mark.parametrize(
    ("sql", "write"),
    [
        ("SELECT 1", False),
        ("  -- note\n  select count() FROM t", False),
        ("/* why */ SET max_threads = 1", False),
        ("SHOW TABLES", False),
        ("EXISTS TABLE t", False),
        ("ALTER TABLE t UPDATE x = 1 WHERE 1", True),
        ("INSERT INTO t SELECT 1", True),
        ("WITH source AS (SELECT 1) INSERT INTO t SELECT * FROM source", True),
        ("CREATE TABLE t (x UInt8) ENGINE = Memory", True),
        ("", True),
    ],
)
def test_only_reads_and_session_statements_skip_the_wait(sql, write):
    assert is_write(sql) is write


def test_zero_parts_left_is_not_completion():
    waiter = _waiter([_row("h1", "t", "mutation_1.txt", done=0, parts=0)])
    with pytest.raises(WaitTimeout, match="mutation_1.txt host h1; parts_to_do=0"):
        waiter.wait()


def test_a_replica_without_the_mutation_is_still_pending():
    waiter = _waiter([_row("r1", "t", "0000000001", done=1)], hosts={"t": ("r1", "r2")})
    with pytest.raises(WaitTimeout, match="0000000001 host r2; not received yet"):
        waiter.wait()


def test_a_failed_mutation_stops_with_the_scoped_kill():
    waiter = _waiter([_row("h1", "t", "mutation_2.txt", reason="Cannot parse 'n/a'")])
    with pytest.raises(MutationFailed) as raised:
        waiter.wait(before_run=True)
    failure = raised.value.failure
    assert failure.foreign and failure.reason == "Cannot parse 'n/a'"
    assert failure.kill.endswith("AND table = 't' AND mutation_id = 'mutation_2.txt'")


def _waiter(rows, hosts=None):
    state = SimpleNamespace(database="db", health_cluster=None)
    budget = WaitBudget(timeout=0, display=WaitDisplay())
    waiter = MutationWaiter(None, state, budget)
    # The real wait loop runs against these stubs: the rows the server returns, and which
    # hosts each table's mutations must finish on.
    waiter._rows = lambda expected: [waiter._row(row) for row in rows]
    waiter._resolve_hosts = lambda table: (hosts or {}).get(table)
    return waiter


def _row(host, table, mutation, done=0, parts=1, reason=""):
    return (host, table, mutation, done, parts, reason, [])
