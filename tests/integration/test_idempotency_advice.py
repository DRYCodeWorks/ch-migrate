"""Every rewrite the gate suggests passes the gate and survives a retry on ClickHouse."""

import secrets
import time

import pytest

from ch_migrate.idempotency import classify_idempotency

pytestmark = pytest.mark.integration

SETUP = (
    "CREATE TABLE {db}.src (p UInt8, id UInt64, old String) "
    "ENGINE = MergeTree PARTITION BY p ORDER BY id",
    "INSERT INTO {db}.src VALUES (1, 1, 'a'), (1, 2, 'b'), (2, 3, 'c')",
    "CREATE TABLE {db}.dst AS {db}.src",
)

# The statement the gate flags, the text its advice must quote, and the advised rewrite.
REWRITES = [
    (
        "CREATE TABLE {db}.t (id UInt64) ENGINE = MergeTree ORDER BY id",
        "CREATE TABLE IF NOT EXISTS {db}.t",
        ["CREATE TABLE IF NOT EXISTS {db}.t (id UInt64) ENGINE = MergeTree ORDER BY id"],
    ),
    (
        "DROP TABLE {db}.dst",
        "DROP TABLE IF EXISTS {db}.dst",
        ["DROP TABLE IF EXISTS {db}.dst"],
    ),
    (
        "ALTER TABLE {db}.src ADD COLUMN region String",
        "ADD COLUMN IF NOT EXISTS region",
        ["ALTER TABLE {db}.src ADD COLUMN IF NOT EXISTS region String"],
    ),
    (
        "ALTER TABLE {db}.src DROP COLUMN old",
        "DROP COLUMN IF EXISTS old",
        ["ALTER TABLE {db}.src DROP COLUMN IF EXISTS old"],
    ),
    (
        "ALTER TABLE {db}.src RENAME COLUMN old TO legacy",
        "RENAME COLUMN IF EXISTS old TO legacy",
        ["ALTER TABLE {db}.src RENAME COLUMN IF EXISTS old TO legacy"],
    ),
    (
        "ALTER TABLE {db}.src ADD INDEX idx id TYPE minmax GRANULARITY 1",
        "ADD INDEX IF NOT EXISTS idx",
        ["ALTER TABLE {db}.src ADD INDEX IF NOT EXISTS idx id TYPE minmax GRANULARITY 1"],
    ),
    (
        "RENAME TABLE {db}.dst TO {db}.renamed",
        "RENAME TABLE IF EXISTS {db}.dst TO {db}.renamed",
        ["RENAME TABLE IF EXISTS {db}.dst TO {db}.renamed"],
    ),
    (
        "ALTER TABLE {db}.dst ATTACH PARTITION 1 FROM {db}.src",
        "REPLACE PARTITION 1 FROM {db}.src",
        ["ALTER TABLE {db}.dst REPLACE PARTITION 1 FROM {db}.src"],
    ),
    (
        "INSERT INTO {db}.dst SELECT * FROM {db}.src",
        "TRUNCATE TABLE {db}.dst",
        ["TRUNCATE TABLE {db}.dst", "INSERT INTO {db}.dst SELECT * FROM {db}.src"],
    ),
    (
        "ALTER TABLE {db}.src DETACH PARTITION 1",
        "",
        ["ALTER TABLE {db}.src DETACH PARTITION 1", "ALTER TABLE {db}.src ATTACH PARTITION 1"],
    ),
]


@pytest.mark.parametrize(
    ("flagged", "quoted", "rewrite"), REWRITES, ids=[flagged for flagged, _, _ in REWRITES]
)
def test_advised_rewrite_converges_after_a_failure_anywhere(
    project, request, flagged, quoted, rewrite
):
    if quoted:
        assert quoted in classify_idempotency(flagged).suggestion
    for statement in rewrite:
        if not statement.startswith("INSERT"):  # an INSERT still carries its waiver
            assert classify_idempotency(statement).status == "ok", statement
    clean = _state_after(project, request, [rewrite])
    for failed_after in range(1, len(rewrite) + 1):
        retried = _state_after(project, request, [rewrite[:failed_after], rewrite])
        assert retried == clean, (failed_after, retried, clean)


def _state_after(project, request, runs):
    database = f"{project.database}_{secrets.token_hex(4)}"
    request.addfinalizer(lambda: project.client.command(f"DROP DATABASE IF EXISTS {database} SYNC"))
    project.client.command(f"CREATE DATABASE {database}")
    for statements in ((*SETUP,), *runs):
        for statement in statements:
            project.client.command(statement.format(db=database))
        _settle(project.client, database)
    columns = project.client.query(
        "SELECT table, name, type FROM system.columns WHERE database = {db:String} "
        "ORDER BY table, name",
        parameters={"db": database},
    ).result_rows
    indexes = project.client.query(
        "SELECT table, name FROM system.data_skipping_indices WHERE database = {db:String} "
        "ORDER BY table, name",
        parameters={"db": database},
    ).result_rows
    tables = sorted({table for table, _, _ in columns})
    rows = [project.client.command(f"SELECT count() FROM {database}.`{table}`") for table in tables]
    return columns, indexes, rows


def _settle(client, database):
    """Wait out this attempt's mutations, as a person fixing a failed migration would.

    On Cloud, a column an unfinished DROP or RENAME COLUMN mutation touches refuses the
    next change to it (code 36), even with IF EXISTS, until that mutation completes.
    """
    pending = "SELECT count() FROM system.mutations WHERE database = {db:String} AND NOT is_done"
    for _ in range(120):
        if not client.command(pending, parameters={"db": database}):
            return
        time.sleep(1)
    raise AssertionError(f"mutations in {database} did not finish")
