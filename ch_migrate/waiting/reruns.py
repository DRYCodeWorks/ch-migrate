"""Stop a rerun of an interrupted migration step that ch-migrate can't vouch for.

Before a step runs, `up` and `down` record that it started; once its version is recorded,
the record goes. A record still there on the next run means the step was interrupted, so
some of its statements may already have applied. The step then runs again on its own only
if every statement in it passes the idempotency gate without a waiver. Otherwise the run
stops until someone checks what the earlier attempt changed and names the revision with
`--rerun-interrupted`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from clickhouse_connect.driver.binding import quote_identifier

from ch_migrate.idempotency import classify_idempotency, waiver_reason
from ch_migrate.statements import MigrationStatement, migration_statements
from ch_migrate.waiting.errors import WaitingError
from ch_migrate.waiting.sql import sql_string

TABLE = "_ch_migrate_in_progress"
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Step:
    revision: str
    direction: str  # "upgrade" or "downgrade"
    path: Path


@dataclass(frozen=True)
class Unvouched:
    """A statement in an interrupted step that might not be safe to run again."""

    statement: MigrationStatement
    why: str


class InterruptedStep(WaitingError):
    """An interrupted step has statements that might not be safe to run again."""

    def __init__(self, step: Step, unvouched: list[Unvouched]):
        self.step, self.unvouched = step, unvouched
        listed = "; ".join(f"{_where(item.statement)} ({item.why})" for item in unvouched)
        super().__init__(
            f"{step.revision[:8]} was interrupted on an earlier run, and ch-migrate can't vouch "
            f"for running it again: {listed}. Check what the earlier run changed, then run it "
            f"again with --rerun-interrupted {step.revision[:8]} "
            f"(with alembic, -x rerun_interrupted={step.revision[:8]})."
        )


class RerunGuard:
    """Records each step as it starts and refuses an unsafe rerun of an interrupted one."""

    def __init__(self, client, state, acknowledged: frozenset[str]):
        self.client, self.state, self.acknowledged = client, state, acknowledged
        self.before_first_upgrade = None  # set by env.py when the gate hasn't run yet
        self._ready = False

    def steps(self, original):
        """Wrap Alembic's step iterator: check and record each step, clear it once recorded."""

        def iterate(heads, context):
            for step in original(heads, context):
                if not hasattr(step, "revision"):
                    yield step  # Stamp steps run no migration body.
                    continue
                direction = "upgrade" if step.is_upgrade else "downgrade"
                current = Step(step.revision.revision, direction, Path(step.revision.path))
                self.begin(current)
                # Alembic asks for the next step only once this one's version is recorded.
                yield step
                self.finish(current)

        return iterate

    def begin(self, step: Step) -> None:
        if step.direction == "upgrade" and self.before_first_upgrade is not None:
            check, self.before_first_upgrade = self.before_first_upgrade, None
            check()
        self._prepare()
        if self._started(step):
            unvouched = unvouched_statements(step.path, step.direction)
            if unvouched and not self._acknowledged(step.revision):
                raise InterruptedStep(step, unvouched)
            logger.warning(
                f"{step.revision[:8]} was interrupted on an earlier run; running it again"
            )
        self._record(step)

    def finish(self, step: Step) -> None:
        self.client.command(
            f"ALTER TABLE {self._table()} DELETE WHERE revision = {sql_string(step.revision)} "
            "SETTINGS mutations_sync = 2"
        )

    def _acknowledged(self, revision: str) -> bool:
        return any(len(given) >= 4 and revision.startswith(given) for given in self.acknowledged)

    def _prepare(self) -> None:
        if self._ready:
            return
        if not self.client.command(f"EXISTS TABLE {self._table()}"):
            self.client.command(self._create_sql())
        if self._replicated():
            # Fetch markers another replica wrote; there are no mutations here to wait for.
            self.client.command(f"SYSTEM SYNC REPLICA {self._table()} LIGHTWEIGHT")
        self._ready = True

    def _started(self, step: Step) -> bool:
        return bool(
            self.client.command(
                f"SELECT count() FROM {self._table()} "
                "WHERE revision = {revision:String} AND direction = {direction:String}",
                parameters={"revision": step.revision, "direction": step.direction},
                settings={"select_sequential_consistency": 1} if self._shared() else None,
            )
        )

    def _record(self, step: Step) -> None:
        self.client.command(
            f"INSERT INTO {self._table()} (revision, direction) "
            "SELECT {revision:String}, {direction:String}",
            parameters={"revision": step.revision, "direction": step.direction},
            # Replicated and Shared MergeTree drop a block identical to an earlier insert.
            settings={
                "insert_deduplication_token": f"chm_started_{uuid4().hex}",
                "async_insert": 0,
            },
        )

    def _create_sql(self) -> str:
        engine, on_cluster = "MergeTree", ""
        if self.state.database_engine == "Replicated":
            engine = "ReplicatedMergeTree"
        elif not self._shared() and self.state.cluster:
            path = f"/clickhouse/ch_migrate/{quote(self.state.database, safe='')}/{{shard}}/{TABLE}"
            engine = f"ReplicatedMergeTree({sql_string(path)}, '{{replica}}')"
            on_cluster = f" ON CLUSTER {quote_identifier(self.state.on_cluster)}"
        return (
            f"CREATE TABLE IF NOT EXISTS {self._table()}{on_cluster} (revision String, "
            "direction LowCardinality(String), started DateTime64(3) DEFAULT now64(3)) "
            f"ENGINE = {engine} ORDER BY (revision, direction)"
        )

    def _table(self) -> str:
        return f"{quote_identifier(self.state.database)}.{quote_identifier(TABLE)}"

    def _shared(self) -> bool:
        return self.state.database_engine.startswith("Shared")

    def _replicated(self) -> bool:
        return self.state.database_engine == "Replicated" or (
            not self._shared() and bool(self.state.cluster)
        )


def unvouched_statements(path: Path, direction: str) -> list[Unvouched]:
    """Statements in this step the gate can't vouch for: waived, unreadable, or unsafe."""
    found = []
    for statement in migration_statements(path):
        if statement.direction != direction:
            continue
        reason = waiver_reason(statement.comments)
        if reason is not None:
            found.append(Unvouched(statement, f"waiver: {reason}" if reason else "empty waiver"))
        elif not statement.readable:
            found.append(Unvouched(statement, "SQL that only exists at run time"))
        elif classify_idempotency(statement.sql).status != "ok":
            found.append(Unvouched(statement, "not safe to run twice"))
    return found


def _where(statement: MigrationStatement) -> str:
    return f"{statement.source}:{statement.line}"
