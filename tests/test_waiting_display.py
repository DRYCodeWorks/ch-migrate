"""What `up` shows while it waits: the terminal bar, piped lines and failure reports."""

import io

import pytest
from rich.console import Console

from ch_migrate.migration_runner import Upgrade, _report_failure
from ch_migrate.runner import _Progress
from ch_migrate.sql import SqlStatementError
from ch_migrate.waiting_display import DDLProgress, HostParts, MutationProgress, WaitDisplay
from ch_migrate.waiting_types import FailedMutation, MutationFailed, UnknownOutcome, WaitTimeout

GIB = 1 << 30


@pytest.fixture
def clock(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("ch_migrate.waiting_display.time.monotonic", lambda: now[0])
    return now


def _display(terminal):
    buffer = io.StringIO()
    console = Console(file=buffer, force_terminal=terminal, width=160, color_system=None)
    return WaitDisplay(console), buffer


def _mutation(left, host="h1", sizes=True):
    names = frozenset(f"all_{i}_{i}_0" for i in range(left))
    return HostParts(host, names, left, {name: GIB for name in names} if sizes else None)


def _progress(*hosts):
    return MutationProgress("demo_dev.events", "7", hosts)


def test_terminal_bar_weights_by_bytes_and_estimates_time(clock):
    display, buffer = _display(terminal=True)
    display.start_migration("3f9c2a1b  backfill_status")
    display.update(_progress(_mutation(41)))
    clock[0] = 172.0
    display.update(_progress(_mutation(18)))
    assert "23.0 / 41.0 GiB · 23/41 parts · 1:12 · ~0:56 left" in buffer.getvalue()
    assert "━" in buffer.getvalue()
    clock[0] = 223.0
    display.complete("demo_dev.events")
    assert (
        buffer.getvalue()
        .rstrip()
        .endswith("✓ 3f9c2a1b  backfill_status  mutation 7 · 41 parts · 41.0 GiB · 2:03")
    )


def test_piped_lines_start_then_every_tenth_or_thirty_seconds(clock):
    display, buffer = _display(terminal=False)
    display.start_migration("3f9c2a1b  backfill_status")
    for at, left in ((100, 40), (101, 38), (102, 36), (140, 35)):
        clock[0] = float(at)
        display.update(_progress(_mutation(left, sizes=False)))
    clock[0] = 141.0
    display.complete("demo_dev.events")
    assert buffer.getvalue().splitlines() == [
        "  waiting on mutation 7 (demo_dev.events): 0/40 parts",
        "  waiting on mutation 7 (demo_dev.events): 4/40 parts · 0:02",
        "  waiting on mutation 7 (demo_dev.events): 5/40 parts · 0:40",
        "✓ 3f9c2a1b  backfill_status  mutation 7 · 40 parts · 0:41",
    ]
    assert "\x1b" not in buffer.getvalue() and "\r" not in buffer.getvalue()


def test_unreadable_part_sizes_fall_back_to_parts_once():
    display, buffer = _display(terminal=True)
    assert display.wants_bytes
    display.parts_only("can't read system.parts (ACCESS_DENIED)")
    display.parts_only("can't read system.parts (ACCESS_DENIED)")
    assert buffer.getvalue().count("! Progress counts parts only") == 1
    assert not display.wants_bytes


def test_parts_only_bar_has_no_time_estimate(clock):
    display, buffer = _display(terminal=True)
    display.update(_progress(_mutation(10, sizes=False)))
    clock[0] = 110.0
    display.update(_progress(_mutation(4, sizes=False)))
    assert "6/10 parts" in buffer.getvalue() and "left" not in buffer.getvalue()


def test_each_replica_gets_its_own_row(clock):
    display, buffer = _display(terminal=True)
    display.update(_progress(_mutation(5, "ch-1"), _mutation(5, "ch-2")))
    clock[0] = 105.0
    display.update(_progress(_mutation(2, "ch-2")))
    text = buffer.getvalue()
    assert "ch-1" in text and "ch-2" in text
    display.complete("demo_dev.events")
    assert "· 2 replicas" in buffer.getvalue().splitlines()[-1]


def test_on_cluster_ddl_counts_hosts(clock):
    display, buffer = _display(terminal=True)
    display.update(DDLProgress("query-0000000012", 2, 3))
    assert "2/3 hosts" in buffer.getvalue()
    piped, lines = _display(terminal=False)
    piped.update(DDLProgress("query-0000000012", 2, 3))
    assert lines.getvalue() == "  waiting on ON CLUSTER DDL query-0000000012: 2/3 hosts\n"


def test_failed_mutation_names_reason_retry_and_kill(capsys):
    failure = FailedMutation(
        "demo_dev.events",
        "mutation_7.txt",
        "h1",
        "Cannot parse string 'n/a' as UInt32",
        "KILL MUTATION WHERE database = 'demo_dev' AND table = 'events' AND mutation_id = 'mutation_7.txt'",
    )
    error = _statement_error(MutationFailed("failed", failure))
    _report_failure(Upgrade("dev"), _current("3f9c2a1b  widen_amount"), error)
    err = capsys.readouterr().err
    assert err.startswith(
        "✗ 3f9c2a1b  widen_amount (migrations/sql/x.up.sql:2) failed: mutation mutation_7.txt "
        "on demo_dev.events failed on h1"
    )
    assert "Cannot parse string 'n/a' as UInt32" in err
    assert "keeps retrying a failed mutation" in err and failure.kill in err


def test_timeout_says_the_work_continues_and_how_to_resume(capsys):
    timeout = WaitTimeout(
        "Timed out", "demo_dev.events mutation mutation_7.txt; parts_to_do=3", 3.0
    )
    _report_failure(Upgrade("dev"), _current("3f9c2a1b  backfill"), _statement_error(timeout))
    err = capsys.readouterr().err
    assert err.startswith("✗ Timed out after 3.0s waiting on 3f9c2a1b  backfill")
    assert "Pending: demo_dev.events mutation mutation_7.txt; parts_to_do=3" in err
    assert "ch-migrate up dev again to keep waiting; it won't reissue the statement" in err


def test_unknown_outcome_shows_the_operator_guidance(capsys):
    unknown = UnknownOutcome("Outcome unknown for demo_dev.events\nRead-only inspection: SELECT 1")
    _report_failure(Upgrade("dev"), _current("3f9c2a1b  backfill"), _statement_error(unknown))
    err = capsys.readouterr().err
    assert "outcome unknown, so the statement was not reissued" in err
    assert "  Read-only inspection: SELECT 1" in err


def _current(label):
    progress = _Progress()
    progress.current, progress.count = label, 1
    return progress


def _statement_error(cause):
    try:
        raise SqlStatementError(
            f"migrations/sql/x.up.sql (statement 1 of 1, line 2): {cause}"
        ) from cause
    except SqlStatementError as error:
        return error
