"""Journal actual cursor writes and wait before Alembic completes a revision."""

from __future__ import annotations

import hashlib
import json
import sys
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from uuid import uuid4

from alembic.script import ScriptDirectory
from clickhouse_connect.driver.exceptions import DatabaseError
from sqlalchemy import event

from ch_migrate.classify import classify
from ch_migrate.hooks import run_hooks
from ch_migrate.idempotency import classify_idempotency
from ch_migrate.introspect import ColumnDefinition, Schema, TableDefinition, _parse_order_by
from ch_migrate.statements import migration_statements
from ch_migrate.waiting_mutations import MutationWaiter
from ch_migrate.waiting_sql import (
    bind_statement,
    is_session_or_read,
    modifies_ttl,
    mutation_sql,
    qualified_table,
    query_setting,
    query_settings,
    sql_string,
    statement_cluster,
    statement_digest,
    statement_table,
)
from ch_migrate.waiting_store import StepKey, WaitingJournal
from ch_migrate.waiting_types import UnknownOutcome, WaitBudget, WaitingError
from ch_migrate.waiting_versions import VersionWrites


class MigrationWaiter:
    """One connection, one invocation deadline, and ordered durable receipts."""

    def __init__(self, connection, state, timeout=None):
        self.connection, self.state = connection, state
        self.client = connection.connection.dbapi_connection.client
        self.budget = WaitBudget(timeout)
        self.journal = WaitingJournal(connection, state, self.budget)
        self.mutations = MutationWaiter(self.client, state, self.budget)
        self.versions = VersionWrites(self)
        self.progress_stream = sys.stderr
        self.revision = None
        self.generation = self.position = self._internal = 0
        self.fingerprint = ""
        self._pending = {}
        self._batch_key = None
        self._listeners = []
        self._scripts = None

    def install(self, config) -> None:
        self._scripts = ScriptDirectory.from_config(config)
        for target, name, listener, options in (
            (self.connection, "before_cursor_execute", self._before, {"retval": True}),
            (self.connection, "after_cursor_execute", self._after, {}),
            (self.connection.engine, "handle_error", self._error, {}),
        ):
            event.listen(target, name, listener, **options)
            self._listeners.append((target, name, listener))

    def close(self) -> None:
        self.budget.finish()
        for target, name, listener in reversed(self._listeners):
            event.remove(target, name, listener)
        self._listeners.clear()
        self.revision = None

    @contextmanager
    def internal(self):
        self._internal += 1
        try:
            yield
        finally:
            self._internal -= 1

    def reserve(self, payload: dict) -> tuple[StepKey, dict, bool]:
        self.position += 1
        key = StepKey(self.revision, self.generation, self.position)
        previous = self.journal.read(key)
        payload["fingerprint"] = self.fingerprint
        if previous is None:
            payload["token"] = "chm_mutation_" + uuid4().hex
            return key, payload, True
        retryable = (
            previous.get("phase") == "rejected"
            and previous.get("repeat_safe")
            and payload.get("repeat_safe")
        )
        if previous["digest"] != payload["digest"] and not retryable:
            raise UnknownOutcome(
                f"Revision {key.revision} statement {key.position} changed after execution began; "
                "statement not reissued. Reconcile its journal and the original SQL."
            )
        if retryable:
            payload["token"] = "chm_mutation_" + uuid4().hex
        return key, payload if retryable else previous, bool(retryable)

    def validate_revision(self, revision: str, expected: str) -> None:
        script = self._scripts.get_revision(revision)
        if script is None or _fingerprint(Path(script.path)) != expected:
            raise UnknownOutcome(
                f"Revision {revision} changed during incomplete version bookkeeping; reconcile before resuming"
            )

    def unknown_outcome(self, key: StepKey, record: dict, error: Exception) -> UnknownOutcome:
        receipt = record.get("receipt") or record.get("before_target") or {}
        table = record.get("table")
        if record.get("kind") == "version":
            table = (self.state.database, "alembic_version")
            receipt = next((step["receipt"] for step in record["steps"] if "receipt" in step), {})
        message = (
            f"{error}\nRevision {key.revision}, generation {key.generation}, statement {key.position}; "
            f"token {record.get('token', receipt.get('token', 'unrecorded'))}; "
            f"recorded target UUIDs {json.dumps(receipt.get('uuids', {}), sort_keys=True)}.\n"
            "Stop migration runners. Establish the actual outcome independently; reconcile only this "
            "journal key before rerunning. Do not stamp the revision or blindly delete its journal.\n"
            f"Read-only inspection: SELECT sequence, payload FROM {self.journal.table} "
            f"WHERE revision = {sql_string(key.revision)} AND generation = {key.generation} "
            f"AND position = {key.position} ORDER BY sequence DESC;\n"
        )
        if table:
            source = (
                f"clusterAllReplicas({sql_string(receipt['cluster'])}, system.mutations)"
                if receipt.get("cluster")
                else "system.mutations"
            )
            message += (
                "Read-only inspection: SELECT mutation_id, command, is_done, parts_to_do, latest_fail_reason "
                f"FROM {source} WHERE database = {sql_string(table[0])} AND table = {sql_string(table[1])};"
            )
        return UnknownOutcome(message)

    def steps(self, original, hooks):
        def iterate(heads, context):
            for step in original(heads, context):
                if not hasattr(step, "revision"):
                    yield step  # Native Alembic stamp steps have no migration body to track.
                    continue
                revision = step.revision.revision
                fingerprint = _fingerprint(Path(step.revision.path))
                if step.is_upgrade:
                    self._begin(revision, fingerprint)
                else:
                    self.journal.invalidate(revision, fingerprint)
                execute = step.migration_fn
                step.migration_fn = self._revision_body(execute, hooks, revision)
                try:
                    yield step
                finally:
                    self.revision = None

        return iterate

    def run_pre_hooks(self, hooks) -> None:
        fingerprint = hashlib.sha256(json.dumps(hooks.pre_migrate).encode()).hexdigest()
        self._begin("__batch__", fingerprint)
        key = StepKey(self.revision, self.generation, 0)
        if self.journal.read(key).get("finished"):
            self.journal.invalidate(self.revision, fingerprint)
            self._begin("__batch__", fingerprint)
            key = StepKey(self.revision, self.generation, 0)
        self._batch_key = key
        run_hooks(
            self.connection,
            hooks.pre_migrate,
            db=self.state.database,
            phase="pre_migrate",
            revision="all",
        )
        boundary, record, _ = self.reserve(
            {"kind": "boundary", "phase": "done", "digest": "pre-hooks-complete"}
        )
        self.journal.write(boundary, record)
        self.revision = None

    def finish_run(self) -> None:
        if self._batch_key:
            control = self.journal.read(self._batch_key)
            control["finished"] = True
            self.journal.write(self._batch_key, control)

    def _begin(self, revision: str, fingerprint: str) -> None:
        self.revision, self.fingerprint, self.position = revision, fingerprint, 0
        self.generation = self.journal.begin(revision, fingerprint)
        self._completed_targets = {}
        for key, record in self.journal.records(revision, self.generation):
            if (
                record["phase"] not in ("done", "rejected")
                and record.get("fingerprint") != fingerprint
            ):
                raise UnknownOutcome(
                    f"Revision {revision} changed with unresolved work; statement not reissued"
                )
            if record["phase"] == "accepted" and record["kind"] == "write":
                self._complete(key, record)
            if record["phase"] == "done" and record.get("table"):
                self._completed_targets[tuple(record["table"])] = record.get("after_target")

    def _revision_body(self, execute, hooks, revision):
        @wraps(execute)
        def run(**kwargs):
            execute(**kwargs)
            run_hooks(
                self.connection,
                hooks.post_migrate,
                db=self.state.database,
                phase="post_migrate",
                revision=revision,
            )

        return run

    def _before(self, connection, cursor, statement, parameters, context, executemany):
        # SQLAlchemy's event signature is an external contract.
        if self._internal or self.revision is None:
            return statement, parameters
        sample = parameters[0] if executemany and parameters else parameters
        sql = bind_statement(statement, sample, self.client)
        if is_session_or_read(sql):
            return statement, parameters
        if executemany:
            return self._execute_many(statement, parameters, context.execution_options)
        table = statement_table(sql, self.state.database)
        payload = {
            "kind": "write",
            "phase": "intent",
            "digest": statement_digest(statement, parameters),
            "table": list(table) if table else None,
            "repeat_safe": classify_idempotency(sql).status == "ok",
        }
        key, record, fresh = self.reserve(payload)
        if not fresh and self._resume(key, record):
            return "SELECT 1 WHERE 0", {}
        target = self.mutations.target(table) if table else None
        record["before_target"] = target
        kind = self._work_kind(sql, target, sample) if target else None
        if kind:
            token = record["token"]
            record["kind"] = kind
            record["receipt"] = self.mutations.prepare(target, token)
            record["receipt"]["expected_markers"] = 1
            if kind == "ttl":
                record.update(kind="ttl", work_submitted=False, cluster=statement_cluster(sql))
                statement = query_settings(
                    statement, {"materialize_ttl_after_modify": "0", "alter_sync": "0"}
                )
            else:
                statement = mutation_sql(statement, token)
        self.journal.write(key, record)  # Acknowledged intent MUST precede submission.
        self._pending[id(context)] = (key, record)
        return statement, parameters

    def _execute_many(self, statement, parameters, execution_options):
        # Re-enter the single-statement cursor path, preserving statement order
        # and an independently durable intent for each parameter set.
        for binding in parameters:
            self.connection.exec_driver_sql(
                statement, binding, execution_options=execution_options
            ).close()
        return "SELECT 1 WHERE 0", [{}]

    def _after(self, connection, cursor, statement, parameters, context, executemany):
        pending = self._pending.pop(id(context), None)
        if pending is None:
            return
        key, record = pending
        record["phase"] = "accepted"
        self.journal.write(key, record)
        if record["kind"] in ("mutation", "ttl"):
            self._wait_work(key, record)
        self._complete(key, record)

    def _error(self, context):
        if context.connection is not self.connection:
            return
        pending = self._pending.pop(id(context.execution_context), None)
        if pending is None:
            return
        key, record = pending
        # A server rejection of repeat-safe metadata can be repaired and retried.
        # Transport loss and mutation submissions remain unknown, never replayed.
        if (
            record["kind"] == "write"
            and record["repeat_safe"]
            and isinstance(context.original_exception, DatabaseError)
            and context.original_exception.code is not None
        ):
            record["phase"] = "rejected"
            self.journal.write(key, record)

    def _resume(self, key: StepKey, record: dict) -> bool:
        if record["phase"] == "done":
            self._check_completed_target(record)
            return True
        if record["kind"] == "mutation" or (
            record["kind"] == "ttl" and record["phase"] == "accepted"
        ):
            print(
                f"Resuming {key.revision} statement {key.position}; not reissuing SQL",
                file=self.progress_stream,
            )
            self._wait_work(key, record)
            self._complete(key, record)
            return True
        if record["phase"] == "accepted":
            self._complete(key, record)
            return True
        error = UnknownOutcome("Outcome unknown; statement not reissued; migration incomplete")
        raise self.unknown_outcome(key, record, error) from error

    def _wait_work(self, key: StepKey, record: dict) -> None:
        if record["kind"] == "ttl" and not record["work_submitted"]:
            record["work_submitted"] = True
            self.journal.write(key, record)
            cluster = (
                f" ON CLUSTER {sql_string(record['cluster'])}" if record.get("cluster") else ""
            )
            sql = f"ALTER TABLE {qualified_table(tuple(record['table']))}{cluster} MATERIALIZE TTL"
            with self.internal():
                self.connection.exec_driver_sql(mutation_sql(sql, record["receipt"]["token"]))
        try:
            self.mutations.wait(record["receipt"], lambda: self.journal.write(key, record))
        except UnknownOutcome as error:
            raise self.unknown_outcome(key, record, error) from error

    def _complete(self, key: StepKey, record: dict) -> None:
        if record.get("table"):
            record["after_target"] = self.mutations.target(tuple(record["table"]))
            self._completed_targets[tuple(record["table"])] = record["after_target"]
        record["phase"] = "done"
        self.journal.write(key, record)

    def _check_completed_target(self, record: dict) -> None:
        if not record.get("table"):
            return
        expected = self._completed_targets.get(tuple(record["table"]), record.get("after_target"))
        actual = self.mutations.target(tuple(record["table"]))
        if (
            (expected is None) != (actual is None)
            or expected is not None
            and expected["uuids"] != actual["uuids"]
        ):
            raise UnknownOutcome(
                "Completed statement's target was dropped or replaced; reconcile before skipping its SQL"
            )

    def _work_kind(self, sql: str, target: dict, parameters) -> str | None:
        if not target["engine"].endswith("MergeTree"):
            return None
        rows = self.client.query(
            "SELECT name, type FROM system.columns WHERE database = {db:String} AND table = {table:String}",
            parameters={"db": target["database"], "table": target["table"]},
        ).result_rows
        table = TableDefinition(
            target["table"],
            target["engine"],
            columns=[ColumnDefinition(*row) for row in rows],
            order_by=_parse_order_by(target["sorting_key"]) if target["sorting_key"] else [],
        )
        schema = Schema(database=target["database"], tables={target["table"]: table})
        ttl = modifies_ttl(sql)
        materialize = self._materializes_ttl(sql, parameters) if ttl else False
        analyzed = (
            query_settings(sql, {"materialize_ttl_after_modify": "0"})
            if ttl and not materialize
            else sql
        )
        if classify(analyzed, schema).kind != "mutation":
            return None
        return "ttl" if ttl and materialize else "mutation"

    def _materializes_ttl(self, sql: str, parameters) -> bool:
        value = query_setting(sql, "materialize_ttl_after_modify")
        if value is None:
            value = str(self.client.command("SELECT getSetting('materialize_ttl_after_modify')"))
        if value.startswith("{") and isinstance(parameters, dict):
            value = str(parameters[value[1:].split(":", 1)[0]])
        if value.lower() not in ("0", "1", "true", "false"):
            raise WaitingError("materialize_ttl_after_modify must resolve to a boolean value")
        return value.lower() in ("1", "true")


def _fingerprint(path: Path) -> str:
    digest = hashlib.sha256(path.read_bytes())
    for statement in migration_statements(path):
        digest.update(statement.sql.encode())
    return digest.hexdigest()
