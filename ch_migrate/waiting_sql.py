"""SQL identity and ownership markers without matching another writer's command."""

from __future__ import annotations

import hashlib
import json
import re

from clickhouse_connect.driver.binding import bind_query, quote_identifier

from ch_migrate.classify import _identifier, _tokens
from ch_migrate.sql import _skip_quoted_or_comment
from ch_migrate.waiting_types import WaitingError

_WORD = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")
_MANAGED = frozenset(("mutations_sync", "alter_sync", "lightweight_deletes_sync"))


def bind_statement(statement, parameters, client) -> str:
    bound, _ = bind_query(statement, parameters or None, client.server_tz)
    return bound.decode() if isinstance(bound, bytes) else bound


def statement_digest(statement: str, parameters) -> str:
    serialized = json.dumps(
        [statement, parameters], sort_keys=True, default=str, separators=(",", ":")
    )
    return hashlib.sha256(serialized.encode()).hexdigest()


def statement_table(statement: str, database: str) -> tuple[str, str] | None:
    tokens = _tokens(statement)
    words = tuple(token.upper() for token in tokens)
    if words[:2] in (("ALTER", "TABLE"), ("INSERT", "INTO"), ("DELETE", "FROM")):
        index = 2
    elif words[:1] == ("UPDATE",):
        index = 1
    elif words[:1] in (("CREATE",), ("DROP",), ("RENAME",), ("TRUNCATE",)):
        index = next(
            (i + 1 for i, token in enumerate(words) if token in ("TABLE", "VIEW", "DICTIONARY")),
            len(tokens),
        )
    else:
        return None
    while index < len(tokens) and words[index] in ("IF", "NOT", "EXISTS"):
        index += 1
    if index >= len(tokens):
        return None
    components = [_identifier(tokens[index])]
    while index + 2 < len(tokens) and tokens[index + 1] == ".":
        components.append(_identifier(tokens[index + 2]))
        index += 2
    if any(component.startswith("{") for component in components):
        raise WaitingError("Mutation tracking requires resolved database/table identifiers")
    return (components[-2] if len(components) > 1 else database, components[-1])


def statement_cluster(statement: str) -> str | None:
    tokens = _tokens(statement)
    for index in range(len(tokens) - 2):
        if tuple(token.upper() for token in tokens[index : index + 2]) == ("ON", "CLUSTER"):
            return tokens[index + 2].strip("'`\"")
    return None


def is_session_or_read(statement: str) -> bool:
    tokens = _tokens(statement)
    if tokens and tokens[0].upper() == "WITH":
        operation = next(
            (
                span[0].upper()
                for span in _spans(statement)
                if span[3] == 0 and span[0].upper() in ("SELECT", "INSERT", "UPDATE", "DELETE")
            ),
            None,
        )
        return operation == "SELECT"
    return not tokens or tokens[0].upper() in (
        "SELECT",
        "SHOW",
        "DESCRIBE",
        "DESC",
        "EXPLAIN",
        "SET",
        "USE",
    )


def modifies_ttl(statement: str) -> bool:
    tokens = _tokens(statement)
    return any(
        tuple(word.upper() for word in tokens[index : index + 2]) == ("MODIFY", "TTL")
        for index in range(len(tokens) - 1)
    )


def mutation_sql(statement: str, token: str) -> str:
    """Carry ownership into system.mutations and make submission asynchronous."""
    body, settings = _query_parts(statement)
    words = _tokens(body)
    marker = f"{sql_string(token)} = {sql_string(token)}"
    overrides = {name: "0" for name in _MANAGED}
    if tuple(word.upper() for word in words[:2]) == ("DELETE", "FROM"):
        where = next(
            (span for span in _spans(body) if span[0].upper() == "WHERE" and span[3] == 0), None
        )
        if where is None:
            raise WaitingError("A lightweight DELETE needs a WHERE predicate to track ownership")
        body = body[: where[2]] + f" ({body[where[2]:].strip()}) AND ({marker})"
    elif tuple(word.upper() for word in words[:2]) == ("ALTER", "TABLE"):
        if modifies_ttl(body):
            spans = list(_spans(body))
            start = next(
                span[1]
                for index, span in enumerate(spans[:-1])
                if span[0].upper() == "MODIFY"
                and spans[index + 1][0].upper() == "TTL"
                and span[3] == 0
            )
            body = body[:start].rstrip() + f" DELETE WHERE 0 AND ({marker}), " + body[start:]
            overrides["materialize_ttl_after_modify"] = "0"
        else:
            body += f", DELETE WHERE 0 AND ({marker})"
    else:
        raise WaitingError("Cannot attach a mutation ownership marker to this statement")
    return _with_settings(body, settings, overrides)


def query_settings(statement: str, overrides: dict[str, str]) -> str:
    body, settings = _query_parts(statement)
    return _with_settings(body, settings, overrides)


def query_setting(statement: str, name: str) -> str | None:
    _, settings = _query_parts(statement)
    for setting in reversed(settings):
        key, _, value = setting.partition("=")
        if _identifier(key.strip()).lower() == name:
            return value.strip().strip("'")
    return None


def sql_string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def qualified_table(table: tuple[str, str]) -> str:
    return ".".join(quote_identifier(part) for part in table)


def _query_parts(statement: str) -> tuple[str, list[str]]:
    statement = _without_comments(statement)
    spans = list(_spans(statement))
    if not spans:
        raise WaitingError("Empty statement cannot be tracked")
    sql = statement[: spans[-1][2]].rstrip()
    candidates = [
        span
        for index, span in enumerate(spans[:-2])
        if span[0].upper() == "SETTINGS"
        and span[3] == 0
        and _WORD.fullmatch(_identifier(spans[index + 1][0]))
        and spans[index + 2][0] == "="
    ]
    if not candidates:
        return sql, []
    settings = candidates[-1]
    return sql[: settings[1]].rstrip(), _split_settings(sql[settings[2] :])


def _split_settings(sql: str) -> list[str]:
    starts = [span[1] for span in _spans(sql) if span[0] == "," and span[3] == 0]
    result, previous = [], 0
    for index in starts:
        result.append(sql[previous:index].strip())
        previous = index + 1
    if sql[previous:].strip():
        result.append(sql[previous:].strip())
    return result


def _with_settings(body: str, settings: list[str], overrides: dict[str, str]) -> str:
    retained = [
        item
        for item in settings
        if _identifier(item.split("=", 1)[0].strip()).lower() not in overrides
    ]
    retained.extend(f"{name} = {value}" for name, value in sorted(overrides.items()))
    return body + " SETTINGS " + ", ".join(retained)


def _without_comments(sql: str) -> str:
    chunks, index = [], 0
    while index < len(sql):
        end = _skip_quoted_or_comment(sql, index)
        if end is None:
            chunks.append(sql[index])
            index += 1
        else:
            chunks.append(sql[index:end] if sql[index] in "'\"`$" else " ")
            index = end
    return "".join(chunks)


def _spans(sql: str):
    """Yield significant tokens with offsets/depth, retaining quoted SQL verbatim."""
    index = depth = 0
    while index < len(sql):
        end = _skip_quoted_or_comment(sql, index)
        if end is not None:
            if sql[index] in "'\"`$":
                yield sql[index:end], index, end, depth
            index = end
            continue
        match = _WORD.match(sql, index)
        if match:
            yield match.group(), index, match.end(), depth
            index = match.end()
            continue
        char = sql[index]
        if char in ")]":
            depth -= 1
        if not char.isspace() and char != ";":
            yield char, index, index + 1, depth
        if char in "([":
            depth += 1
        index += 1
