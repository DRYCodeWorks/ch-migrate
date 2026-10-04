"""Ownership SQL and deadline/completion boundaries that could silently replay work."""

import pytest
from click.testing import CliRunner

from ch_migrate.cli import main
from ch_migrate.waiting_mutations import MutationWaiter
from ch_migrate.waiting_sql import (
    is_session_or_read,
    mutation_sql,
    query_setting,
    statement_table,
)
from ch_migrate.waiting_types import WaitBudget, WaitTimeout


@pytest.mark.parametrize("timeout", ["0", "-1", "nan", "inf", "-inf"])
def test_wait_timeout_rejects_invalid_values_before_project_access(tmp_path, monkeypatch, timeout):
    # Click's public command signature, not a database mock, decides usage errors.
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["up", "not-configured", "--timeout", timeout])
    assert result.exit_code == 2
    assert "--timeout" in result.output


def test_mutation_settings_override_ignores_comment_and_literal_keywords():
    sql = (
        "ALTER TABLE db.t UPDATE value = 'SETTINGS -- not a comment', settings=settings+1 "
        "WHERE id=1 -- trailing predicate comment\n"
        "SETTINGS /* policy */ mutations_sync=2, max_threads=3 -- trailing setting comment\n;"
    )
    rewritten = mutation_sql(sql, "owned-token")
    assert query_setting(rewritten, "mutations_sync") == "0"
    assert query_setting(rewritten, "max_threads") == "3"
    assert "'SETTINGS -- not a comment'" in rewritten
    assert "settings=settings+1 WHERE id=1" in rewritten
    assert "DELETE WHERE 0" in rewritten


def test_settings_column_is_not_a_settings_clause():
    rewritten = mutation_sql("ALTER TABLE db.t UPDATE settings=2 WHERE settings=1", "owned-token")
    assert "UPDATE settings=2 WHERE settings=1" in rewritten
    assert query_setting(rewritten, "mutations_sync") == "0"


def test_qualified_identifiers_preserve_dots_inside_quoted_names():
    assert statement_table(
        'ALTER TABLE "data.base".`some.table` UPDATE x=1 WHERE 1', "default"
    ) == ("data.base", "some.table")


@pytest.mark.parametrize(
    "sql,readonly",
    [
        ("WITH 1 AS x SELECT x", True),
        ("WITH source AS (SELECT 1) INSERT INTO t SELECT * FROM source", False),
        ("INSERT INTO t SELECT 'SELECT'", False),
        ("SET max_threads=3", True),
    ],
)
def test_statement_tracking_distinguishes_reads_from_writes(sql, readonly):
    assert is_session_or_read(sql) is readonly


def test_deadline_is_shared_across_waits(monkeypatch):
    now = [10.0]
    monkeypatch.setattr("ch_migrate.waiting_types.time.monotonic", lambda: now[0])
    budget = WaitBudget(timeout=2, started=now[0])
    now[0] = 11.5
    assert budget.elapsed == 1.5
    now[0] = 12.0
    with pytest.raises(WaitTimeout, match="second mutation"):
        budget.check("second mutation")


def test_zero_parts_is_not_mutation_completion(monkeypatch):
    waiter = MutationWaiter(None, None, WaitBudget(timeout=0))
    receipt = {
        "database": "db",
        "table": "t",
        "engine": "MergeTree",
        "initiator": "host",
        "token": "owned",
        "uuids": {"host": "uuid"},
        "ids": {"host": ["mutation_1.txt"]},
        "done_hosts": [],
        "baseline": {"host": []},
    }
    monkeypatch.setattr(waiter, "_identities", lambda _: {"host": "uuid"})
    monkeypatch.setattr(waiter, "_rows", lambda _: [("host", "mutation_1.txt", "owned", 0, 0, "")])
    with pytest.raises(WaitTimeout, match="mutation_1.txt"):
        waiter.wait(receipt, lambda: None)
    assert receipt["done_hosts"] == []
