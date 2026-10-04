"""Read-only planning of pending upgrades against the current live schema."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from graphlib import TopologicalSorter
from pathlib import Path
from typing import Any

from clickhouse_connect.driver.binding import quote_identifier
from rich.console import Console

from ch_migrate.classify import (
    Classification,
    _alter_actions,
    _identifier,
    _name,
    _order_expressions,
    _split_settings,
    _table_definition,
    _tokens,
    classify,
)
from ch_migrate.config import get_env_config, load_config
from ch_migrate.connection import get_client
from ch_migrate.introspect import DependencyGraph, Schema, get_dependencies, get_live_schema
from ch_migrate.json_output import lint_document
from ch_migrate.lint import LintConfig, lint_migrations
from ch_migrate.rebase import build_revision_graph
from ch_migrate.rebuild_ddl import build_definition
from ch_migrate.rebuild_preflight import RebuildRequest, inspect_rebuild
from ch_migrate.rebuild_types import RebuildOptions
from ch_migrate.statements import (
    MigrationStatement,
    migration_statements,
    pending_revisions,
)
from ch_migrate.version_table import (
    VersionTableState,
    assert_version_mutations_healthy,
    inspect_version_table,
)


@dataclass(frozen=True)
class RewriteSize:
    precision: str
    compressed_bytes: int
    uncompressed_bytes: int
    part_count: int
    columns: list[str]
    partition_id: str | None


def build_plan(root: Path, environment: str) -> dict[str, Any]:
    """Resolve pending revisions without importing them or issuing a read barrier."""
    versions = root / "migrations" / "versions"
    if not versions.is_dir():
        raise ValueError("migrations/versions/ not found")
    config_path = root / "config.yaml"
    lint_config = LintConfig.from_config(load_config(config_path))
    env = get_env_config(environment, config_path)
    client = get_client(env)
    try:
        return _build_document(client, _PlanInput(root, environment, env, lint_config))
    finally:
        client.close()


def render_plan(document: dict[str, Any]) -> None:
    """Render the same facts used by JSON; no independent inspection or estimates."""
    console = Console(markup=False, highlight=False, soft_wrap=True)
    console.print(f"Plan: {document['environment']} / {document['database']}")
    for warning in document["warnings"]:
        console.print(f"Warning: {warning}")
    for migration in document["migrations"]:
        console.print(f"\nRevision {migration['revision']} ({migration['path']})", style="bold")
        for statement in migration["statements"]:
            _render_statement(console, statement)
    console.print("\nLint findings:")
    for finding in document["findings"]:
        _render_finding(console, finding)
    console.print(
        "Counts: " + ", ".join(f"{key}={value}" for key, value in document["counts"].items())
    )
    console.print("Gate would refuse up: " + ("yes" if document["gate_would_refuse"] else "no"))


@dataclass(frozen=True)
class _PlanInput:
    root: Path
    environment: str
    env: dict[str, Any]
    lint_config: LintConfig


@dataclass(frozen=True)
class _Scope:
    client: Any
    schema: Schema
    dependencies: DependencyGraph
    env: dict[str, Any]
    findings: list[dict[str, Any]]
    deployment: VersionTableState
    revision: str = ""


@dataclass(frozen=True)
class _Rewrite:
    database: str
    table: str
    columns: tuple[str, ...]
    partition_id: str | None


def _build_document(client: Any, request: _PlanInput) -> dict[str, Any]:
    versions = request.root / "migrations" / "versions"
    graph = build_revision_graph(versions)
    heads, warnings, deployment = _read_heads(client, request.env)
    pending = pending_revisions(graph, heads)
    report = lint_migrations(
        versions,
        config=request.lint_config,
        client=client,
        database=request.env["database"],
        revisions=pending,
    )
    lint = lint_document(report)
    database = request.env["database"]
    scope = _Scope(
        client,
        get_live_schema(client, database),
        get_dependencies(client, database),
        request.env,
        lint["findings"],
        deployment,
    )
    ordered = TopologicalSorter(
        {rev: item.down_revisions for rev, item in graph.migrations.items()}
    )
    migrations = []
    for revision in ordered.static_order():
        if revision not in graph.migrations:
            raise ValueError(f"Missing parent revision {revision}")
        if revision not in pending:
            continue
        migration = graph.migrations[revision]
        revision_scope = replace(scope, revision=revision)
        statements = [
            _plan_statement(statement, revision_scope)
            for statement in migration_statements(migration.path)
            if statement.direction == "upgrade"
        ]
        migrations.append(
            {
                "revision": revision,
                "path": str(migration.path.relative_to(request.root)),
                "statements": statements,
            }
        )
    return {
        "database": database,
        "environment": request.environment,
        "warnings": warnings,
        "gate_would_refuse": bool(lint["counts"]["blocking"]),
        **lint,
        "migrations": migrations,
    }


def _read_heads(client: Any, env: dict[str, Any]) -> tuple[set[str], list[str], VersionTableState]:
    state = inspect_version_table(client, env["database"], env.get("cluster"))
    assert_version_mutations_healthy(client, state)
    warnings = [state.warning()] if state.warning() else []
    if not state.table_engine:
        return set(), warnings, state
    rows = client.query(
        "SELECT count() FROM system.mutations WHERE database = {db:String} "
        "AND table = 'alembic_version' AND is_done = 0",
        parameters={"db": env["database"]},
    ).result_rows
    if rows[0][0]:
        raise RuntimeError("Version bookkeeping is unfinished; reconcile it before planning")
    final = " FINAL" if state.table_engine.endswith("ReplacingMergeTree") else ""
    heads = client.query(
        f"SELECT version_num FROM {quote_identifier(env['database'])}.alembic_version{final}"
    )
    if "Replicated" in state.table_engine or "Shared" in state.table_engine:
        warnings.append(
            "Read-only plan observes the connected replica; it does not synchronize replicas or freeze concurrent migrations."
        )
    return {row[0] for row in heads.result_rows}, warnings, state


def _plan_statement(statement: MigrationStatement, scope: _Scope) -> dict[str, Any]:
    sql = statement.sql.replace("{db}", scope.env["database"])
    if scope.env.get("cluster"):
        sql = sql.replace("{cluster}", scope.env["cluster"])
    rebuild_call = statement.rebuild
    if rebuild_call:
        rebuild_call = replace(
            rebuild_call, table=rebuild_call.table.replace("{db}", scope.env["database"])
        )
    statement = replace(statement, sql=sql, rebuild=rebuild_call)
    classification = classify(statement, scope.schema)
    table = _table_definition(scope.schema, classification.table or "")
    downstream = scope.dependencies.affected_by_drop(table.name) if table else []
    size = _rewrite_size(sql, classification, scope)
    rebuild = None
    if classification.kind == "rebuild":
        if table is None:
            raise ValueError(f"Cannot inspect rebuild source {classification.table!r}")
        rebuild = asdict(_rebuild_assessment(statement, table, scope))
    findings = [
        item
        for item in scope.findings
        if item["file"] == statement.source and item["line"] == statement.line
    ]
    return {
        "sql": sql,
        "file": statement.source,
        "line": statement.line,
        "classification": asdict(classification),
        "size": asdict(size) if size else None,
        "downstream": [
            {"name": node.name, "type": node.obj_type}
            for node in downstream
            if node.obj_type in ("materialized_view", "dictionary")
        ],
        "findings": findings,
        "rebuild": rebuild,
    }


def _rebuild_assessment(statement: MigrationStatement, table: Any, scope: _Scope):
    allow_async_loss = False
    if statement.rebuild:
        call = statement.rebuild
        options = RebuildOptions(
            scope.env["database"],
            table.name,
            scope.revision,
            0,
            scope.deployment.on_cluster,
            call.select,
            call.allow_unacknowledged_async_loss,
        )
        target = build_definition(table, statement.sql, options).target
        allow_async_loss = call.allow_unacknowledged_async_loss
    else:
        target = _rebuild_target(table, statement.sql)
    request = RebuildRequest(
        scope.env["database"], table, target, scope.deployment.health_cluster, allow_async_loss
    )
    return inspect_rebuild(scope.client, request)


def _rewrite_size(sql: str, classification: Classification, scope: _Scope) -> RewriteSize | None:
    tokens = _tokens(sql)
    if not classification.table or (
        classification.kind != "mutation" and tokens[0].upper() != "UPDATE"
    ):
        return None
    database, _, table = classification.table.rpartition(".")
    columns = _exact_columns(tokens, scope.schema)
    definition = _table_definition(scope.schema, classification.table)
    partition = _partition_id(tokens, definition)
    request = _Rewrite(database or scope.env["database"], table, columns, partition)
    return _measure_rewrite(scope.client, request)


def _measure_rewrite(client: Any, request: _Rewrite) -> RewriteSize:
    parameters = {"db": request.database, "table": request.table}
    predicate = "database = {db:String} AND table = {table:String} AND active = 1"
    if request.partition_id is not None:
        predicate += " AND partition_id = {partition:String}"
        parameters["partition"] = request.partition_id
    parts = client.query(
        "SELECT sum(data_compressed_bytes), sum(data_uncompressed_bytes), count(), "
        "countIf(part_type = 'Compact') FROM system.parts WHERE " + predicate,
        parameters=parameters,
    ).result_rows[0]
    # Compact parts share one data file; per-column counters are zero, not exact
    # rewrite bytes. Keep the whole-part ceiling rather than claiming zero work.
    exact = bool(request.columns) and not parts[3]
    row = parts
    if exact:
        parameters["columns"] = list(request.columns)
        row = client.query(
            "SELECT sum(column_data_compressed_bytes), sum(column_data_uncompressed_bytes), "
            "uniqExact(name) FROM system.parts_columns WHERE "
            + predicate
            + " AND column IN {columns:Array(String)}",
            parameters=parameters,
        ).result_rows[0]
    return RewriteSize(
        "exact" if exact else "ceiling",
        int(row[0]),
        int(row[1]),
        int(row[2]),
        list(request.columns),
        request.partition_id,
    )


def _alter_clauses(tokens: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    index = 4 if tuple(token.upper() for token in tokens[2:4]) == ("IF", "EXISTS") else 2
    _, index = _name(tokens, index)
    if tuple(token.upper() for token in tokens[index : index + 2]) == ("ON", "CLUSTER"):
        index += 3
    actions, _ = _split_settings(tokens[index:])
    return _alter_actions(actions)


def _exact_columns(tokens: tuple[str, ...], schema: Schema) -> tuple[str, ...]:
    if tuple(token.upper() for token in tokens[:2]) != ("ALTER", "TABLE"):
        return ()
    columns = []
    table, _ = _name(tokens, 4 if tokens[2:4] == ("IF", "EXISTS") else 2)
    actions = _alter_clauses(tokens)
    if len(actions) > 1 and any(
        "PARTITION" in (token.upper() for token in action) for action in actions
    ):
        return ()
    for action in actions:
        if classify(f"ALTER TABLE {table} " + " ".join(action), schema).kind == "metadata":
            continue
        words = tuple(token.upper() for token in action)
        if words[:2] not in (("MODIFY", "COLUMN"), ("DROP", "COLUMN"), ("CLEAR", "COLUMN")):
            return ()
        index = 4 if words[2:4] == ("IF", "EXISTS") else 2
        if len(action) <= index:
            return ()
        columns.append(_identifier(action[index]))
    return tuple(dict.fromkeys(columns))


def _partition_id(tokens: tuple[str, ...], table: Any) -> str | None:
    words = tuple(token.upper() for token in tokens)
    explicit = [
        index
        for index in range(len(words) - 3)
        if words[index : index + 3] == ("IN", "PARTITION", "ID")
    ]
    if explicit:
        values = {tokens[index + 3].strip("'") for index in explicit}
        # Mixed ALTER actions may affect the whole table; never understate their ceiling.
        actions = _alter_clauses(tokens) if words[:2] == ("ALTER", "TABLE") else (tokens,)
        if len(values) == 1 and all(
            "PARTITION" in tuple(token.upper() for token in action) for action in actions
        ):
            return values.pop()
    if "WHERE" not in words:
        return None
    predicate, _ = _split_settings(tokens[words.index("WHERE") + 1 :])
    # Only an entire simple equality can narrow the ceiling. OR, casts and tuples remain table-wide.
    if len(predicate) == 3 and predicate[1] == "=" and _identifier(predicate[0]) == "_partition_id":
        if predicate[2].startswith("'") and predicate[2].endswith("'"):
            return predicate[2][1:-1]
    # Numeric identity partition keys have the same printable ID as their value.
    if table and len(predicate) >= 3 and predicate[-2] == "=" and predicate[-1].isdigit():
        key_tokens = _tokens(table.partition_by or "")
        if key_tokens[:2] in (("toYYYYMM", "("), ("toYYYYMMDD", "("), ("toYear", "(")):
            if predicate[:-2] == key_tokens:
                return predicate[-1]
        key = table.partition_by or ""
        column = next((col for col in table.columns if col.name == key), None)
        if (
            len(predicate) == 3
            and column
            and _identifier(predicate[0]) == key
            and column.type.startswith("UInt")
        ):
            return predicate[-1]
    return None


def _rebuild_target(source: Any, sql: str):
    target = replace(source)
    for action in _alter_clauses(_tokens(sql)):
        words = tuple(token.upper() for token in action)
        if words[:3] == ("MODIFY", "PARTITION", "BY"):
            target = replace(target, partition_by=" ".join(action[3:]))
        elif words[:3] == ("MODIFY", "ORDER", "BY"):
            target = replace(target, order_by=_order_expressions(action[3:]))
        elif words[:2] == ("MODIFY", "ENGINE"):
            target = replace(
                target, engine="".join(action[3:] if action[2:3] == ("=",) else action[2:])
            )
    return target


def _render_statement(console: Console, statement: dict[str, Any]) -> None:
    classification = statement["classification"]
    console.print(f"  {statement['file']}:{statement['line']}")
    console.print("  " + statement["sql"])
    console.print(
        f"  {classification['kind']} / {classification['table']}: {classification['detail']}"
    )
    size = statement["size"]
    if size:
        prefix = "exact" if size["precision"] == "exact" else "ceiling: up to"
        console.print(
            f"  {prefix} {size['compressed_bytes']} compressed bytes, {size['uncompressed_bytes']} uncompressed bytes, {size['part_count']} parts"
        )
        console.print(
            f"  Columns: {', '.join(size['columns']) or 'all'}; partition ID: {size['partition_id'] or 'all'}"
        )
    for node in statement["downstream"]:
        console.print(f"  Downstream {node['type']}: {node['name']}")
    if statement["rebuild"]:
        _render_rebuild(console, statement["rebuild"])
    for finding in statement["findings"]:
        _render_finding(console, finding)


def _render_rebuild(console: Console, rebuild: dict[str, Any]) -> None:
    console.print(
        f"  Rebuild: {rebuild['bytes_on_disk']} bytes on disk; {rebuild['part_count']} parts"
    )
    console.print(f"  Parts per partition: {rebuild['partition_parts']}")
    console.print(f"  Insert rate: {rebuild['insert_rows_per_second']} rows/second")
    console.print("  " + rebuild["engine_note"])
    for finding in rebuild["findings"]:
        console.print(
            f"  {finding['severity']} [{finding['code']}]: {finding['message']} {finding['details']}"
        )


def _render_finding(console: Console, finding: dict[str, Any]) -> None:
    console.print(
        f"  {finding['severity']} [{finding['rule']}] {finding['file']}:{finding['line']}: {finding['message']}"
    )
    console.print(f"  Blocking: {finding['blocking']}; waiver: {finding['waived']}")
