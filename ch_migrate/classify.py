"""Classify ClickHouse work without executing it; the server corpus is authoritative."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from ch_migrate.idempotency import _ACTIONS, _action_clauses
from ch_migrate.introspect import Schema, TableDefinition
from ch_migrate.sql import _skip_quoted_or_comment
from ch_migrate.statements import MigrationStatement

_WORD_OR_NUMBER = re.compile(r"[A-Za-z_][A-Za-z_0-9]*|\d+(?:\.\d+)?")
_TYPE_MODIFIERS = {"DEFAULT", "MATERIALIZED", "ALIAS", "CODEC", "COMMENT", "REMOVE", "TTL"}


@dataclass(frozen=True)
class Classification:
    kind: Literal["metadata", "mutation", "rebuild", "other"]
    table: str | None
    detail: str


def classify(
    statement: str | MigrationStatement, live_schema: Schema | None = None
) -> Classification:
    """Classify metadata, background mutations, impossible ALTERs, or other work.

    The contract is tests/corpus/classification.yaml. Its real-server proof in
    tests/integration/test_classification.py defines correctness, not a guessed
    list of SQL verbs. With no live type to compare, MODIFY COLUMN is conservatively
    a mutation *if the type changes*. Unsupported actions remain explicitly other.
    """
    if isinstance(statement, MigrationStatement) and statement.rebuild is not None:
        return Classification("rebuild", statement.rebuild.table, "Guarded online table rebuild")
    tokens = _tokens(statement if isinstance(statement, str) else statement.sql)
    if not tokens:
        return Classification("other", None, "No SQL statement")
    words = tuple(token.upper() for token in tokens)
    if words[:2] == ("ALTER", "TABLE"):
        return _classify_alter(tokens, live_schema)
    table = _statement_target(tokens)
    if words[0] in ("CREATE", "DROP", "RENAME"):
        return Classification("metadata", table, "Object metadata operation")
    if words[:2] == ("DELETE", "FROM"):
        return Classification(
            "mutation", table, "Lightweight DELETE rewrites the row-existence mask"
        )
    if words[0] == "UPDATE":
        return Classification(
            "other",
            table,
            "Synchronous lightweight UPDATE writes patch parts; no background mutation record",
        )
    return Classification("other", table, "Not a classified schema operation")


@dataclass(frozen=True)
class _Alter:
    table: str
    definition: TableDefinition | None
    actions: tuple[tuple[str, ...], ...]
    settings: tuple[str, ...]


def _classify_alter(tokens: tuple[str, ...], schema: Schema | None) -> Classification:
    index = 2
    if tuple(token.upper() for token in tokens[index : index + 2]) == ("IF", "EXISTS"):
        index += 2
    table, index = _name(tokens, index)
    if tuple(token.upper() for token in tokens[index : index + 2]) == ("ON", "CLUSTER"):
        index += 3
    actions, settings = _split_settings(tokens[index:])
    definition = _table_definition(schema, table)
    alter = _Alter(table, definition, _alter_actions(actions), settings)
    classifications = [_classify_action(action, alter) for action in alter.actions]
    priority = {"metadata": 0, "mutation": 1, "other": 2, "rebuild": 3}
    kind = max(classifications, key=lambda item: priority[item.kind]).kind
    return Classification(
        kind, table, "; ".join(dict.fromkeys(item.detail for item in classifications))
    )


def _alter_actions(tokens: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    actions: list[tuple[str, ...]] = []
    for clause in _action_clauses(tokens):
        starts_action = clause and clause[0].upper() in _ACTIONS | {"REMOVE"}
        if actions and (not starts_action or clause[1:2] == ("=",)):
            actions[-1] += (",",) + clause
        else:
            actions.append(clause)
    return tuple(actions)


def _classify_action(action: tuple[str, ...], alter: _Alter) -> Classification:
    words = tuple(token.upper() for token in action)
    prefix = words[:2]
    if prefix == ("MODIFY", "COLUMN"):
        return _modify_column(action[2:], alter)
    if words[:3] == ("MODIFY", "ORDER", "BY"):
        return _modify_order(action[3:], alter)
    if prefix in (("MODIFY", "PRIMARY"), ("MODIFY", "PARTITION"), ("MODIFY", "ENGINE")):
        return Classification(
            "rebuild", alter.table, "This key or engine cannot be changed by an in-place ALTER"
        )
    if prefix == ("MODIFY", "TTL"):
        disabled = _setting_is_zero(alter.settings, "materialize_ttl_after_modify")
        return Classification(
            "metadata" if disabled else "mutation",
            alter.table,
            "TTL metadata only" if disabled else "MODIFY TTL materializes existing parts",
        )
    if words[:1] in (("UPDATE",), ("DELETE",), ("CLEAR",), ("MATERIALIZE",)):
        return Classification("mutation", alter.table, "Background mutation of existing parts")
    if prefix in (
        ("DROP", "COLUMN"),
        ("RENAME", "COLUMN"),
        ("DROP", "INDEX"),
        ("DROP", "PROJECTION"),
    ):
        return Classification(
            "mutation",
            alter.table,
            "Column, index, or projection change creates a background mutation",
        )
    if words[:1] in (("ADD",), ("COMMENT",), ("RESET",)) or prefix in (
        ("MODIFY", "SETTING"),
        ("REMOVE", "TTL"),
    ):
        return Classification("metadata", alter.table, "Metadata change; no background mutation")
    return Classification(
        "other", alter.table, "ALTER action is not covered by the server-backed classification"
    )


def _modify_column(tokens: tuple[str, ...], alter: _Alter) -> Classification:
    if tuple(token.upper() for token in tokens[:2]) == ("IF", "EXISTS"):
        tokens = tokens[2:]
    if not tokens:
        return Classification("other", alter.table, "Missing column definition")
    column = _identifier(tokens[0])
    type_tokens = _column_type(tokens[1:])
    if not type_tokens:
        return Classification(
            "metadata", alter.table, "Column default, comment, or codec metadata only"
        )
    current = (
        next((item for item in alter.definition.columns if item.name == column), None)
        if alter.definition
        else None
    )
    if current is None:
        return Classification(
            "mutation", alter.table, "Rewrites existing parts if the type changes"
        )
    if _normalize_type(type_tokens) == _normalize_type(_tokens(current.type)):
        return Classification("metadata", alter.table, "Column type is unchanged")
    return Classification(
        "mutation",
        alter.table,
        f"Column {column} changes type from {current.type} to {''.join(type_tokens)}",
    )


def _modify_order(tokens: tuple[str, ...], alter: _Alter) -> Classification:
    if not alter.definition:
        return Classification(
            "rebuild",
            alter.table,
            "Sorting-key change requires live schema to prove a new-column-only extension",
        )
    requested = _order_expressions(tokens)
    current = [_normalize_type(_tokens(item)) for item in alter.definition.order_by]
    added = set()
    for action in alter.actions:
        if tuple(token.upper() for token in action[:2]) == ("ADD", "COLUMN"):
            index = (
                5 if tuple(token.upper() for token in action[2:5]) == ("IF", "NOT", "EXISTS") else 2
            )
            if len(action) > index:
                added.add(_normalize_type((action[index],)))
    existing = {_normalize_type((column.name,)) for column in alter.definition.columns}
    suffix = requested[len(current) :]
    if requested[: len(current)] == current and all(item in added - existing for item in suffix):
        return Classification(
            "metadata",
            alter.table,
            "Sorting key unchanged or extended only by columns added in this ALTER",
        )
    return Classification(
        "rebuild", alter.table, "Changing existing sorting-key expressions requires a table rebuild"
    )


def _statement_target(tokens: tuple[str, ...]) -> str | None:
    words = tuple(token.upper() for token in tokens)
    if words[0] == "UPDATE":
        return _name(tokens, 1)[0]
    if words[:2] in (("INSERT", "INTO"), ("DELETE", "FROM")):
        return _name(tokens, 2)[0]
    if words[0] not in ("CREATE", "DROP", "RENAME"):
        return None
    types = {"TABLE", "VIEW", "DICTIONARY", "DATABASE", "USER", "ROLE"}
    index = next((index + 1 for index, token in enumerate(words) if token in types), len(tokens))
    while index < len(words) and words[index] in ("IF", "NOT", "EXISTS"):
        index += 1
    return _name(tokens, index)[0] if index < len(tokens) else None


def _table_definition(schema: Schema | None, name: str) -> TableDefinition | None:
    if schema is None:
        return None
    database, _, table = name.rpartition(".")
    if database and schema.database and database != schema.database:
        return None
    return schema.tables.get(table or name)


def _split_settings(tokens: tuple[str, ...]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    depth = 0
    for index, token in enumerate(tokens):
        if token in ("(", "["):
            depth += 1
        elif token in (")", "]"):
            depth -= 1
        elif token.upper() == "SETTINGS" and depth == 0:
            return tokens[:index], tokens[index + 1 :]
    return tokens, ()


def _setting_is_zero(tokens: tuple[str, ...], name: str) -> bool:
    value = None
    for index in range(len(tokens) - 2):
        if tokens[index].lower() == name and tokens[index + 1] == "=":
            value = tokens[index + 2]
    return value == "0"


def _column_type(tokens: tuple[str, ...]) -> tuple[str, ...]:
    depth = 0
    for index, token in enumerate(tokens):
        if depth == 0 and token.upper() in _TYPE_MODIFIERS:
            return tokens[:index]
        if token in ("(", "["):
            depth += 1
        elif token in (")", "]"):
            depth -= 1
    return tokens


def _order_expressions(tokens: tuple[str, ...]) -> list[str]:
    if tokens[:1] == ("(",) and tokens[-1:] == (")",):
        tokens = tokens[1:-1]
    elif tokens[:2] == ("tuple", "(") and tokens[-1:] == (")",):
        tokens = tokens[2:-1]
    return [_normalize_type(action) for action in _action_clauses(tokens)] if tokens else []


def _normalize_type(tokens: tuple[str, ...]) -> str:
    return "".join(_identifier(token) for token in tokens)


def _name(tokens: tuple[str, ...], index: int) -> tuple[str, int]:
    if index >= len(tokens):
        return "", index
    names = [_identifier(tokens[index])]
    index += 1
    while index + 1 < len(tokens) and tokens[index] == ".":
        names.append(_identifier(tokens[index + 1]))
        index += 2
    return ".".join(names), index


def _identifier(token: str) -> str:
    if token[:1] in ('"', "`") and token[-1:] == token[:1]:
        quote = token[0]
        return token[1:-1].replace(quote * 2, quote).replace("\\" + quote, quote)
    return token


def _tokens(sql: str) -> tuple[str, ...]:
    tokens = []
    index = 0
    while index < len(sql):
        end = _skip_quoted_or_comment(sql, index)
        if end is not None:
            if sql[index] in "'\"`$":
                tokens.append(sql[index:end])
            index = end
            continue
        if sql[index] == "{" and (end := sql.find("}", index + 1)) != -1:
            tokens.append(sql[index : end + 1])
            index = end + 1
            continue
        match = _WORD_OR_NUMBER.match(sql, index)
        if match:
            tokens.append(match.group())
            index = match.end()
            continue
        if not sql[index].isspace() and sql[index] != ";":
            tokens.append(sql[index])
        index += 1
    return tuple(tokens)
