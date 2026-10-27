"""Resume insert-first version bookkeeping without invoking a revision twice."""

from __future__ import annotations

import ast
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.sql.dml import Delete, Insert, Update

from ch_migrate import ui
from ch_migrate.waiting_sql import (
    mutation_sql,
    qualified_table,
    query_settings,
    sql_string,
    statement_digest,
)
from ch_migrate.waiting_types import UnknownOutcome, WaitingError


class VersionWrites:
    def __init__(self, owner):
        self.owner = owner

    def execute(self, construct, impl):
        steps = _version_steps(construct, impl)
        payload = {
            "kind": "version",
            "phase": "intent",
            "steps": steps,
            "digest": statement_digest(str(construct), [step["sql"] for step in steps]),
        }
        key, record, _ = self.owner.reserve(payload)
        self.continue_record(key, record)
        return SimpleNamespace(rowcount=1)

    def resume(self) -> None:
        for key, record in self.owner.journal.unfinished_versions():
            self.owner.validate_revision(key.revision, record["fingerprint"])
            ui.step(f"Resuming version bookkeeping for {key.revision[:8]}", stderr=True)
            self.continue_record(key, record)

    def continue_record(self, key, record: dict) -> None:
        if record["phase"] == "done":
            return
        try:
            for step in record["steps"]:
                if step.get("done"):
                    continue

                def checkpoint() -> None:
                    self.owner.journal.write(key, record)

                if step["kind"] == "insert":
                    self._insert(step, checkpoint)
                else:
                    self._delete(step, checkpoint)
                step["done"] = True
                checkpoint()
            record["phase"] = "done"
            self.owner.journal.write(key, record)
        except UnknownOutcome as error:
            raise self.owner.unknown_outcome(key, record, error) from error

    def _insert(self, step: dict, checkpoint) -> None:
        table = qualified_table((self.owner.state.database, "alembic_version"))
        count = self.owner.client.query(
            f"SELECT count() FROM {table} WHERE version_num = {{version:String}}",
            parameters={"version": step["value"]},
        ).result_rows[0][0]
        if step.get("prepared"):
            if count == 1:
                return  # The exact version-row effect proves the internal INSERT succeeded.
            raise UnknownOutcome(
                f"Outcome unknown for version INSERT {step['value']}: found {count} rows; "
                "statement not reissued; reconcile version state before resuming"
            )
        if count:
            raise WaitingError(
                f"Version {step['value']} already exists before its INSERT; reconcile overlapping heads"
            )
        step["prepared"] = True
        checkpoint()
        self.owner.client.command(
            step["sql"], settings={"async_insert": 0, "wait_for_async_insert": 1}
        )

    def _delete(self, step: dict, checkpoint) -> None:
        if not step.get("prepared"):
            target = self.owner.mutations.target((self.owner.state.database, "alembic_version"))
            if target is None:
                raise UnknownOutcome("Version table disappeared before deleting the previous head")
            token = "chm_version_" + uuid4().hex
            step["receipt"] = self.owner.mutations.prepare(target, token)
            step["prepared"] = True
            checkpoint()
            with self.owner.internal():
                self.owner.connection.exec_driver_sql(mutation_sql(step["sql"], token))
        self.owner.mutations.wait(step["receipt"], checkpoint)


def write_unjournaled(construct, impl):
    """Apply a version INSERT/UPDATE outside a tracked upgrade, such as a downgrade."""
    for step in _version_steps(construct, impl):
        sql = step["sql"]
        if step["kind"] == "delete":
            sql = query_settings(sql, {"mutations_sync": "2"})
        impl._exec(text(sql))
    return SimpleNamespace(rowcount=1)


def _version_steps(construct, impl) -> list[dict]:
    if isinstance(construct, Insert):
        value = _version_value(construct)
        return [
            {"kind": "insert", "value": value, "sql": _insert_sql(impl._version_table_name, value)}
        ]
    if isinstance(construct, Update):
        value = _version_value(construct)
        return [
            {
                "kind": "insert",
                "value": value,
                "sql": _insert_sql(impl._version_table_name, value),
            },
            {
                "kind": "delete",
                "sql": f"ALTER TABLE {impl._version_table_name} DELETE WHERE {impl._compile_version_where(construct)}",
            },
        ]
    if isinstance(construct, Delete):
        return [
            {
                "kind": "delete",
                "sql": f"ALTER TABLE {impl._version_table_name} DELETE WHERE {impl._compile_version_where(construct)}",
            }
        ]
    raise WaitingError("Unsupported version-table transition")


def _insert_sql(table: str, value: str) -> str:
    # Replicated and Shared MergeTree silently drop a block identical to an earlier insert
    # (DRY-1447), so re-inserting a previous head after `down` left no version row at all.
    # A token unique to this write keeps every intentional version row.
    token = sql_string("chm_version_" + uuid4().hex)
    return (
        f"INSERT INTO {table} (version_num) SETTINGS insert_deduplication_token = {token}, "
        f"async_insert = 0 VALUES ({sql_string(value)})"
    )


def _version_value(construct) -> str:
    values = construct._values
    expression = next(iter(values.values())) if values else None
    value = getattr(expression, "value", None)
    if value is None and getattr(expression, "is_literal", False):
        value = ast.literal_eval(str(expression))
    if not isinstance(value, str):
        raise WaitingError("Version-table INSERT/UPDATE must contain a literal revision identifier")
    return value
