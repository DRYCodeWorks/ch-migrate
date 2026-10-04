"""Resume insert-first version bookkeeping without invoking a revision twice."""

from __future__ import annotations

import ast
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy.sql.dml import Delete, Insert, Update

from ch_migrate.waiting_sql import mutation_sql, qualified_table, statement_digest
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
            print(
                f"Resuming version bookkeeping for {key.revision}", file=self.owner.progress_stream
            )
            self.continue_record(key, record)

    def continue_record(self, key, record: dict) -> None:
        if record["phase"] == "done":
            return
        try:
            for step in record["steps"]:
                if step.get("done"):
                    continue
                checkpoint = lambda: self.owner.journal.write(key, record)
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


def _version_steps(construct, impl) -> list[dict]:
    if isinstance(construct, Insert):
        value = _version_value(construct)
        sql = str(construct.compile(dialect=impl.dialect, compile_kwargs={"literal_binds": True}))
        return [{"kind": "insert", "value": value, "sql": sql}]
    if isinstance(construct, Update):
        value = _version_value(construct)
        literal = impl._compile_clause(next(iter(construct._values.values())))
        return [
            {
                "kind": "insert",
                "value": value,
                "sql": f"INSERT INTO {impl._version_table_name} (version_num) VALUES ({literal})",
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


def _version_value(construct) -> str:
    values = construct._values
    expression = next(iter(values.values())) if values else None
    value = getattr(expression, "value", None)
    if value is None and getattr(expression, "is_literal", False):
        value = ast.literal_eval(str(expression))
    if not isinstance(value, str):
        raise WaitingError("Version-table INSERT/UPDATE must contain a literal revision identifier")
    return value
