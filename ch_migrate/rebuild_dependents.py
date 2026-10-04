"""Read-only dependency inspection and pre-copy validation for online rebuilds."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ch_migrate.introspect import get_live_schema, parse_create_dictionary
from ch_migrate.rebuild_ddl import _closing, _ident, _quote, _replace, _tokens, _upper
from ch_migrate.rebuild_types import RebuildDefinition
from ch_migrate.waiting_sql import sql_string
from ch_migrate.waiting_types import WaitingError


class DependentValidationError(WaitingError):
    """A named dependent cannot safely use the replacement table."""


@dataclass(frozen=True)
class DependentQuery:
    name: str
    sql: str
    kind: str


@dataclass(frozen=True)
class DependentInventory:
    source_views: tuple[DependentQuery, ...]
    target_views: tuple[DependentQuery, ...]
    dictionaries: tuple[DependentQuery, ...]


@dataclass(frozen=True)
class DictionaryReload:
    database: str
    names: tuple[str, ...]
    cluster: str | None = None


def inspect_dependents(client: Any, database: str, table: str) -> DependentInventory:
    """Find only same-database objects actually reading or writing this table."""
    schema = get_live_schema(client, database)
    source: list[DependentQuery] = []
    target: list[DependentQuery] = []
    dictionaries: list[DependentQuery] = []
    for name, view in schema.views.items():
        if view.select_query and _references(view.select_query, database, table):
            source.append(DependentQuery(name, view.select_query, "view"))
    for name, view in schema.materialized_views.items():
        if not view.select_query:
            continue
        query = DependentQuery(name, view.select_query, "materialized_view")
        if _references(query.sql, database, table):
            source.append(query)
        if _same_object(view.target_table, database, table):
            target.append(query)
    for name, dictionary in _dictionary_definitions(client, schema, database).items():
        query = _dictionary_query(dictionary, database, table)
        if query is not None:
            dictionaries.append(DependentQuery(name, query, "dictionary"))
    return DependentInventory(tuple(source), tuple(target), tuple(dictionaries))


def validate_dependents(
    client: Any, definition: RebuildDefinition, inventory: DependentInventory
) -> None:
    """Resolve each dependent against the owned replacement without changing data."""
    database, table = definition.database, definition.table
    replacement = table + "__chm_new"
    seen: set[str] = set()
    for dependent in (*inventory.source_views, *inventory.target_views, *inventory.dictionaries):
        try:
            rewritten, count = _rewrite(dependent.sql, _RewriteTarget(database, table, replacement))
            if dependent in inventory.source_views or dependent in inventory.dictionaries:
                if count == 0:
                    raise ValueError("source reference to rebuilt table not found")
            if dependent.kind == "dictionary":
                _validate_dictionary(
                    client, f"{_quote(database)}.{_quote(dependent.name)}", rewritten
                )
            else:
                if dependent.name not in seen:
                    client.query("EXPLAIN PLAN " + rewritten)
                    _header(client, rewritten)
                    seen.add(dependent.name)
                if dependent in inventory.target_views:
                    _validate_target(client, f"{_quote(database)}.{_quote(replacement)}", rewritten)
        except Exception as exc:
            if isinstance(exc, DependentValidationError):
                raise
            raise DependentValidationError(
                f"Cannot validate {dependent.kind} {database}.{dependent.name}: {exc}"
            ) from exc


def reload_dictionaries(client: Any, request: DictionaryReload) -> None:
    """Reload every affected dictionary, propagating its named failure."""
    for name in request.names:
        try:
            cluster = f" ON CLUSTER {_quote(request.cluster)}" if request.cluster else ""
            client.command(
                f"SYSTEM RELOAD DICTIONARY{cluster} " f"{_quote(request.database)}.{_quote(name)}"
            )
        except Exception as exc:
            raise DependentValidationError(
                f"Cannot reload dictionary {request.database}.{name}: {exc}"
            ) from exc


@dataclass(frozen=True)
class _RewriteTarget:
    database: str
    table: str
    replacement: str


def _dictionary_definitions(client: Any, schema: Any, database: str) -> dict:
    """system.dictionaries can be empty even when system.tables has dictionaries."""
    dictionaries = dict(schema.dictionaries)
    rows = client.query(
        "SELECT name FROM system.tables WHERE database = {db:String} " "AND engine = 'Dictionary'",
        parameters={"db": database},
    ).result_rows
    for (name,) in rows:
        if name not in dictionaries:
            ddl = client.query(
                f"SHOW CREATE DICTIONARY {_quote(database)}.{_quote(name)}"
            ).result_rows[0][0]
            parsed = parse_create_dictionary(ddl)
            if parsed is None:
                raise DependentValidationError(f"Cannot parse dictionary {database}.{name}")
            dictionaries[name] = parsed
    return dictionaries


def _dictionary_query(dictionary: Any, database: str, table: str) -> str | None:
    ddl = dictionary.raw_ddl
    tokens = _tokens(ddl)
    source = next((i for i, t in enumerate(tokens) if _upper(t) == "SOURCE"), None)
    if source is None or source + 3 >= len(tokens):
        raise DependentValidationError(
            f"Cannot inspect dictionary {database}.{dictionary.name} SOURCE"
        )
    if _upper(tokens[source + 2]) != "CLICKHOUSE":
        return None
    options = _source_options(tokens, source + 4)
    host = options.get("HOST", "localhost").lower()
    if host not in {"localhost", "127.0.0.1", "::1"}:
        return None
    source_db = options.get("DB")
    source_table = options.get("TABLE")
    if source_table and "." in source_table:
        source_db, source_table = source_table.rsplit(".", 1)
    if source_db != database:
        return None
    if "QUERY" in options:
        query = options["QUERY"]
        return query if _references(query, database, table) else None
    if source_table != table:
        return None
    columns = _dictionary_columns(tokens)
    if not columns:
        raise DependentValidationError(
            f"Cannot inspect dictionary {database}.{dictionary.name} columns"
        )
    return (
        f"SELECT {', '.join(_quote(name) for name in columns)} "
        f"FROM {_quote(database)}.{_quote(table)}"
    )


def _source_options(tokens: tuple, start: int) -> dict[str, str]:
    options: dict[str, str] = {}
    depth = 0
    for index in range(start, len(tokens) - 1):
        text = tokens[index].text
        if text == "(":
            depth += 1
        elif text == ")":
            if depth == 0:
                break
            depth -= 1
        elif depth == 0 and _upper(tokens[index]) and tokens[index + 1].text.startswith("'"):
            value = tokens[index + 1].text[1:-1]
            options[_upper(tokens[index])] = value.replace("\\'", "'").replace("''", "'")
    return options


def _dictionary_columns(tokens: tuple) -> tuple[str, ...]:
    opening = next((i for i, token in enumerate(tokens) if token.text == "("), None)
    if opening is None:
        return ()
    depth = 0
    names: list[str] = []
    for index in range(opening + 1, len(tokens)):
        token = tokens[index]
        if token.text == "(":
            depth += 1
        elif token.text == ")":
            if depth == 0:
                break
            depth -= 1
        elif token.text == "," and depth == 0:
            continue
        elif (index == opening + 1 or tokens[index - 1].text == ",") and depth == 0:
            names.append(_ident(token))
    return tuple(name for name in names if name)


def _same_object(reference: str | None, database: str, table: str) -> bool:
    if reference is None:
        return False
    parts = reference.replace("`", "").split(".")
    return parts == [table] or parts == [database, table]


def _references(sql: str, database: str, table: str) -> bool:
    return _rewrite(sql, _RewriteTarget(database, table, table + "__chm_new"))[1] > 0


def _rewrite(sql: str, target: _RewriteTarget) -> tuple[str, int]:
    """Change FROM/JOIN object references, never text or arbitrary identifiers."""
    database, table, replacement = target.database, target.table, target.replacement
    tokens = _tokens(sql)
    changes: list[tuple[int, int, str]] = []
    for index, token in enumerate(tokens[:-1]):
        if _upper(token) not in {"FROM", "JOIN"}:
            continue
        first = index + 1
        if tokens[first].text == "(":
            continue
        if first + 2 < len(tokens) and tokens[first + 1].text == ".":
            db, name = _ident(tokens[first]), _ident(tokens[first + 2])
            if db == database and name == table:
                changes.append(
                    (
                        tokens[first].start,
                        tokens[first + 2].end,
                        f"{_quote(database)}.{_quote(replacement)}",
                    )
                )
        elif _ident(tokens[first]) == table and (
            first + 1 == len(tokens) or tokens[first + 1].text != "("
        ):
            changes.append(
                (
                    tokens[first].start,
                    tokens[first].end,
                    f"{_quote(database)}.{_quote(replacement)}",
                )
            )
    references = len(changes)
    if references:
        changes.extend(_column_qualifiers(tokens, target, _relation_aliases(tokens)))
    return _replace(sql, changes), references


def _column_qualifiers(tokens: tuple, target: _RewriteTarget, aliases: set[str]) -> list:
    changes = []
    for index, token in enumerate(tokens[:-1]):
        if (
            index + 3 < len(tokens)
            and _ident(token) == target.database
            and tokens[index + 1].text == "."
            and _ident(tokens[index + 2]) == target.table
            and tokens[index + 3].text == "."
        ):
            changes.append(
                (
                    token.start,
                    tokens[index + 2].end,
                    f"{_quote(target.database)}.{_quote(target.replacement)}",
                )
            )
        elif (
            _ident(token) == target.table
            and target.table not in aliases
            and tokens[index + 1].text == "."
            and (index == 0 or tokens[index - 1].text != ".")
        ):
            changes.append((token.start, token.end, _quote(target.replacement)))
    return changes


def _relation_aliases(tokens: tuple) -> set[str]:
    boundaries = {
        "FINAL",
        "SAMPLE",
        "WHERE",
        "PREWHERE",
        "GROUP",
        "ORDER",
        "HAVING",
        "LIMIT",
        "OFFSET",
        "SETTINGS",
        "FORMAT",
        "UNION",
        "EXCEPT",
        "INTERSECT",
        "JOIN",
        "INNER",
        "LEFT",
        "RIGHT",
        "FULL",
        "CROSS",
        "ANY",
        "ALL",
        "ASOF",
        "SEMI",
        "ANTI",
        "GLOBAL",
        "ARRAY",
        "ON",
        "USING",
        "WINDOW",
        "QUALIFY",
    }
    aliases = set()
    for index, token in enumerate(tokens[:-1]):
        if _upper(token) not in {"FROM", "JOIN"}:
            continue
        end = index + 1
        if tokens[end].text == "(":
            end = _closing(tokens, end)
        else:
            while end + 2 < len(tokens) and tokens[end + 1].text == ".":
                end += 2
            if end + 1 < len(tokens) and tokens[end + 1].text == "(":
                end = _closing(tokens, end + 1)
        candidate = end + 1
        if candidate < len(tokens) and _upper(tokens[candidate]) == "AS":
            candidate += 1
        if candidate < len(tokens) and _upper(tokens[candidate]) not in boundaries:
            if alias := _ident(tokens[candidate]):
                aliases.add(alias)
    return aliases


def _header(client: Any, sql: str) -> dict[str, str]:
    result = client.query(f"SELECT * FROM ({sql.rstrip().rstrip(';')}) LIMIT 0")
    return {
        name: getattr(column_type, "name", str(column_type))
        for name, column_type in zip(result.column_names, result.column_types)
    }


def _validate_dictionary(client: Any, qualified_name: str, sql: str) -> None:
    ddl = client.query(f"SHOW CREATE DICTIONARY {qualified_name}").result_rows[0][0]
    tokens = _tokens(ddl)
    columns = _dictionary_columns(tokens)
    client.query("EXPLAIN PLAN " + sql)
    actual = _header(client, sql)
    missing = [name for name in columns if name not in actual]
    if missing:
        raise ValueError(f"missing dictionary key/attribute columns: {missing}")
    expected = _header(client, f"SELECT * FROM {qualified_name}")
    conversions = {name: kind for name, kind in expected.items() if actual[name] != kind}
    _validate_casts(client, sql, conversions)


def _validate_target(client: Any, qualified_name: str, sql: str) -> None:
    actual = _header(client, sql)
    target = _header(client, f"SELECT * FROM {qualified_name}")
    if not actual:
        raise ValueError("TO view SELECT has no output columns")
    for name, source_type in actual.items():
        target_type = target.get(name)
        if target_type is None:
            raise ValueError(f"TO target has no column {name}")
    conversions = {name: target[name] for name, kind in actual.items() if target[name] != kind}
    _validate_casts(client, sql, conversions)


def _validate_casts(client: Any, sql: str, conversions: dict[str, str]) -> None:
    if not conversions:
        return
    projection = ", ".join(
        f"CAST(__chm_input.{_quote(name)}, {sql_string(kind)}) AS {_quote(name)}"
        for name, kind in conversions.items()
    )
    client.query(f"EXPLAIN PLAN SELECT {projection} FROM ({sql}) AS __chm_input")
