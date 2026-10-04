"""Alembic operation entry point for a resumable online table rebuild."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from functools import partial

from alembic.operations import MigrateOperation, Operations

from ch_migrate.classify import _identifier, _tokens
from ch_migrate.connection import get_client
from ch_migrate.introspect import get_create_statement, parse_create_table
from ch_migrate.rebuild_ddl import build_definition
from ch_migrate.rebuild_types import RebuildOptions, RebuildRuntime
from ch_migrate.sql import load_statements
from ch_migrate.waiting_types import WaitingError


@Operations.register_operation("rebuild_table")
class RebuildTableOp(MigrateOperation):
    """One logical journal entry; internal writes use the rebuild recovery machine."""

    def __init__(self, table: str, create_sql_path: str, options: dict):
        self.table = table
        self.create_sql_path = create_sql_path
        self.options = options

    @classmethod
    def rebuild_table(
        cls,
        operations,
        table,
        create_sql_path,
        *,
        select=None,
        allow_unacknowledged_async_loss=False,
    ):
        return operations.invoke(
            cls(
                table,
                create_sql_path,
                {
                    "select": select,
                    "allow_unacknowledged_async_loss": allow_unacknowledged_async_loss,
                },
            )
        )


def rebuild_table(table, create_sql_path, *, select=None, allow_unacknowledged_async_loss=False):
    """Rebuild from one replacement CREATE TABLE in migrations/sql/."""
    from alembic import op

    return op.rebuild_table(
        table,
        create_sql_path,
        select=select,
        allow_unacknowledged_async_loss=allow_unacknowledged_async_loss,
    )


@Operations.implementation_for(RebuildTableOp)
def execute_rebuild_operation(operations: Operations, operation: RebuildTableOp):
    from ch_migrate.rebuild_engine import execute_rebuild

    opts = operations.get_context().opts
    waiter = opts.get("ch_migrate_waiter")
    if waiter is None or waiter.revision is None:
        raise WaitingError(
            "rebuild_table requires a ch-migrate upgrade with the current environment"
        )
    statements = load_statements(operation.create_sql_path)
    if len(statements) != 1:
        raise ValueError("rebuild_table requires exactly one replacement CREATE TABLE statement")
    sql = statements[0].sql
    request = _request(operation, waiter)
    key, record, _ = waiter.reserve(
        {"kind": "rebuild", "phase": "intent", "digest": _digest(request, sql), "repeat_safe": True}
    )
    source = _source(waiter, request, record)
    definition = build_definition(source, sql, request)
    record.setdefault("source_ddl", source.raw_ddl)
    runtime = RebuildRuntime(
        client=waiter.client,
        control_factory=partial(get_client, opts["ch_migrate_env_config"]),
        definition=definition,
        deployment=waiter.state,
        budget=waiter.budget,
        record=record,
        checkpoint=lambda: waiter.journal.write(key, record),
        refresh=lambda: _refresh(waiter, key, record),
    )
    result = execute_rebuild(runtime)
    record.update(phase="done", result=result)
    runtime.checkpoint()
    return result


def _request(operation: RebuildTableOp, waiter) -> RebuildOptions:
    if not isinstance(operation.table, str) or not operation.table:
        raise ValueError("rebuild_table requires a nonempty table name")
    tokens = _tokens(operation.table)
    database = waiter.state.database
    if len(tokens) == 1:
        table = _identifier(tokens[0])
    elif len(tokens) == 3 and tokens[1] == "." and _identifier(tokens[0]) == database:
        table = _identifier(tokens[2])
    else:
        raise ValueError("rebuild_table requires a table in the migration database")
    if not isinstance(operation.options["allow_unacknowledged_async_loss"], bool):
        raise ValueError("allow_unacknowledged_async_loss must be an explicit boolean")
    return RebuildOptions(
        database=database,
        table=table,
        revision=waiter.revision,
        generation=waiter.generation,
        cluster=waiter.state.on_cluster,
        **operation.options,
    )


def _digest(options: RebuildOptions, sql: str) -> str:
    payload = {"options": asdict(options), "sql": sql}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _source(waiter, request: RebuildOptions, record: dict):
    ddl = record.get("source_ddl")
    if ddl is None:
        ddl = get_create_statement(waiter.client, request.database, request.table)
    source = parse_create_table(ddl)
    if source is None:
        raise ValueError("Cannot parse the rebuild source's CREATE TABLE")
    return source


def _refresh(waiter, key, current):
    # Ownership may have changed since the CLI's initial journal snapshot.
    # Read the previous owner's acknowledged checkpoints on this replica.
    waiter.journal.synchronize()
    previous = waiter.journal.read(key)
    if previous and previous.get("phase") == "rejected" and current.get("phase") == "intent":
        return None  # Known pre-copy rejection was cleaned; reserve authorized a fresh attempt.
    return previous
