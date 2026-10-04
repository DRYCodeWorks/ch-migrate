"""Build exact-schema helper DDL for an online ClickHouse table rebuild."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ch_migrate.introspect import ColumnDefinition, TableDefinition
from ch_migrate.rebuild_types import RebuildDefinition, RebuildOptions

_ENGINE = re.compile(
    r"(?:(?:Replicated|Shared))?(?:Replacing|Summing|Aggregating|Collapsing|VersionedCollapsing|Graphite)?MergeTree\Z",
    re.I,
)
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z_0-9]*\Z")
_CLAUSES = {"ENGINE", "PARTITION", "PRIMARY", "ORDER", "SAMPLE", "TTL", "SETTINGS", "COMMENT"}
_COLUMN_EXTRAS = {"INDEX", "PROJECTION", "CONSTRAINT"}


def build_definition(
    source: TableDefinition, requested_sql: str, options: RebuildOptions
) -> RebuildDefinition:
    """Validate the one explicit target DDL and retain its complete SQL text."""
    if source.name != options.table or not source.raw_ddl:
        raise ValueError("source name or SHOW CREATE definition does not match rebuild table")
    target_ddl = _parse(requested_sql, options.database, options.table)
    _parse(source.raw_ddl, options.database, options.table)
    if target_ddl.uuid_span:
        raise ValueError("target CREATE must not specify a UUID; rebuild owns helper UUIDs")
    if target_ddl.cluster_span:
        raise ValueError("target CREATE must not specify ON CLUSTER; deployment selects it")
    columns = tuple(
        column.name
        for column in target_ddl.columns
        if column.default_kind not in {"MATERIALIZED", "ALIAS"}
    )
    if not columns:
        raise ValueError("target needs at least one insertable column")
    projection = _projection(options.select, columns)
    target = TableDefinition(
        name=options.table,
        engine=_engine_text(target_ddl),
        columns=list(target_ddl.columns),
        order_by=_order_columns(target_ddl.clauses.get("ORDER", "")),
        partition_by=target_ddl.clauses.get("PARTITION"),
        ttl=target_ddl.clauses.get("TTL"),
        settings=_settings(target_ddl.clauses.get("SETTINGS", "")),
        raw_ddl=target_ddl.sql,
    )
    return RebuildDefinition(
        database=options.database,
        table=options.table,
        source=source,
        target=target,
        create_sql=target_ddl.sql,
        projection=projection,
        columns=columns,
        cluster=options.cluster,
        revision=options.revision,
        generation=options.generation,
        allow_unacknowledged_async_loss=options.allow_unacknowledged_async_loss,
    )


def helper_ddl(definition: RebuildDefinition, role: str, uuid: str) -> str:
    """Create a same-database physical helper with its own UUID and Keeper path."""
    if role not in {"new", "snap", "stage"}:
        raise ValueError(f"unknown rebuild helper role: {role}")
    _validate_uuid(uuid)
    schema = definition.source if role == "snap" else definition.target
    parsed = _parse(schema.raw_ddl, definition.database, definition.table)
    name = f"{definition.table}__chm_{role}"
    replacements = [
        (
            parsed.tokens[parsed.name_at].start,
            parsed.tokens[parsed.name_at + 2].end,
            f"{_quote(definition.database)}.{_quote(name)}",
        ),
        (parsed.engine_at, parsed.engine_end, _helper_engine(parsed, role, uuid)),
    ]
    if parsed.uuid_span:
        replacements.append((*parsed.uuid_span, f"UUID '{uuid}'"))
    else:
        replacements.append((parsed.schema_at, parsed.schema_at, f"UUID '{uuid}' "))
    if parsed.cluster_span:
        replacements.append((*parsed.cluster_span, ""))
    sql = _replace(parsed.sql, replacements)
    if definition.cluster:
        sql = _insert_cluster(sql, definition.cluster)
    return sql


def dual_ddl(definition: RebuildDefinition, uuid: str) -> str:
    """Mirror incoming source inserts through an explicitly owned view UUID."""
    _validate_uuid(uuid)
    db = _quote(definition.database)
    table = _quote(definition.table)
    cluster = f" ON CLUSTER {_quote(definition.cluster)}" if definition.cluster else ""
    return (
        f"CREATE MATERIALIZED VIEW {db}.{_quote(definition.table + '__chm_dual')}"
        f" UUID '{uuid}'{cluster} TO {db}.{_quote(definition.table + '__chm_new')} "
        f"AS SELECT {definition.projection} FROM {db}.{table}"
    )


@dataclass(frozen=True)
class _Token:
    text: str
    start: int
    end: int


@dataclass(frozen=True)
class _DDL:
    sql: str
    tokens: tuple[_Token, ...]
    name_at: int
    schema_at: int
    schema_end: int
    engine_at: int
    engine_end: int
    engine_name: str
    engine_args: tuple[str, ...] | None
    header_end: int
    uuid_span: tuple[int, int] | None
    cluster_span: tuple[int, int] | None
    columns: tuple[ColumnDefinition, ...]
    clauses: dict[str, str]


def _parse(sql: str, database: str, table: str) -> _DDL:
    text = sql.strip()
    tokens = _tokens(text)
    if tokens and tokens[-1].text == ";":
        text = text[: tokens[-1].start].rstrip()
        tokens = _tokens(text)
    if any(token.text == ";" for token in tokens):
        raise ValueError("rebuild SQL must contain exactly one CREATE TABLE")
    if len(tokens) < 9 or _upper(tokens[0]) != "CREATE" or _upper(tokens[1]) != "TABLE":
        raise ValueError("rebuild SQL must be one CREATE TABLE with an explicit schema")
    if _upper(tokens[2]) in {"IF", "TEMPORARY"}:
        raise ValueError("conditional or temporary CREATE TABLE is unsafe for rebuild")
    if tokens[3].text != "." and _ident(tokens[2]) == table:
        text = (
            text[: tokens[2].start] + f"{_quote(database)}.{tokens[2].text}" + text[tokens[2].end :]
        )
        tokens = _tokens(text)
    if _ident(tokens[2]) != database or tokens[3].text != "." or _ident(tokens[4]) != table:
        raise ValueError("CREATE TABLE must target the requested database and table")
    position, uuid_span, cluster_span = _header(tokens, 5)
    if position >= len(tokens) or tokens[position].text != "(":
        raise ValueError("CREATE TABLE requires an explicit column schema (never AS table)")
    close = _closing(tokens, position)
    columns = _columns(text, tokens, (position, close))
    clauses = _clauses(text, tokens, close + 1)
    engine_at, engine_end, engine_name, args = _engine(text, tokens, close + 1)
    if not _ENGINE.fullmatch(engine_name):
        raise ValueError(f"unsupported rebuild table engine: {engine_name}")
    return _DDL(
        text,
        tokens,
        2,
        tokens[position].start,
        tokens[close].end,
        engine_at,
        engine_end,
        engine_name,
        args,
        tokens[position].start,
        uuid_span,
        cluster_span,
        columns,
        clauses,
    )


def _header(
    tokens: tuple[_Token, ...], position: int
) -> tuple[int, tuple[int, int] | None, tuple[int, int] | None]:
    uuid_span = cluster_span = None
    while position < len(tokens) and tokens[position].text != "(":
        word = _upper(tokens[position])
        if word == "UUID" and uuid_span is None and position + 1 < len(tokens):
            if not _string(tokens[position + 1].text):
                raise ValueError("invalid CREATE TABLE UUID")
            uuid_span = (tokens[position].start, tokens[position + 1].end)
            position += 2
        elif word == "ON" and cluster_span is None and position + 2 < len(tokens):
            if _upper(tokens[position + 1]) != "CLUSTER":
                raise ValueError("unsupported CREATE TABLE header")
            cluster_span = (tokens[position].start, tokens[position + 2].end)
            position += 3
        else:
            raise ValueError("unsupported or ambiguous CREATE TABLE header")
    return position, uuid_span, cluster_span


def _tokens(text: str) -> tuple[_Token, ...]:
    result: list[_Token] = []
    index = 0
    while index < len(text):
        if text[index].isspace():
            index += 1
            continue
        if text.startswith("--", index) or text.startswith("/*", index):
            end = text.find("\n" if text.startswith("--", index) else "*/", index + 2)
            if end == -1:
                if text.startswith("/*", index):
                    raise ValueError("unterminated SQL comment")
                break
            index = end + (0 if text.startswith("--", index) else 2)
            continue
        start = index
        quote = text[index]
        if quote in {"'", '"', "`"}:
            index = _quoted_end(text, index)
        elif quote.isalpha() or quote == "_":
            index += 1
            while index < len(text) and (text[index].isalnum() or text[index] == "_"):
                index += 1
        else:
            index += 1
        result.append(_Token(text[start:index], start, index))
    return tuple(result)


def _quoted_end(text: str, index: int) -> int:
    quote = text[index]
    index += 1
    while index < len(text):
        if text[index] == "\\":
            index += 2
        elif text[index] == quote:
            index += 1
            if index < len(text) and text[index] == quote:
                index += 1
            else:
                return index
        else:
            index += 1
    raise ValueError("unterminated SQL quote")


def _closing(tokens: tuple[_Token, ...], opening: int) -> int:
    depth = 0
    for index in range(opening, len(tokens)):
        if tokens[index].text == "(":
            depth += 1
        elif tokens[index].text == ")":
            depth -= 1
            if depth == 0:
                return index
    raise ValueError("unbalanced CREATE TABLE parentheses")


def _split(tokens: tuple[_Token, ...], start: int, end: int) -> list[tuple[int, int]]:
    groups: list[tuple[int, int]] = []
    depth = 0
    beginning = start
    for index in range(start, end):
        if tokens[index].text in ("(", "[", "{"):
            depth += 1
        elif tokens[index].text in (")", "]", "}"):
            depth -= 1
        elif tokens[index].text == "," and depth == 0:
            groups.append((beginning, index))
            beginning = index + 1
    groups.append((beginning, end))
    if depth or any(a == b for a, b in groups):
        raise ValueError("ambiguous or empty schema expression")
    return groups


def _columns(
    text: str, tokens: tuple[_Token, ...], bounds: tuple[int, int]
) -> tuple[ColumnDefinition, ...]:
    start, end = bounds
    columns: list[ColumnDefinition] = []
    for first, last in _split(tokens, start + 1, end):
        if _upper(tokens[first]) in _COLUMN_EXTRAS:
            continue
        if last - first < 2:
            raise ValueError("column definition needs a name and type")
        columns.append(_column(text, tokens[first:last]))
    if not columns or len({column.name for column in columns}) != len(columns):
        raise ValueError("CREATE TABLE has no columns or duplicate columns")
    return tuple(columns)


def _column(text: str, tokens: tuple[_Token, ...]) -> ColumnDefinition:
    name = _ident(tokens[0])
    if not name:
        raise ValueError("unrecognized column definition")
    modifiers: list[tuple[str, int]] = []
    depth = 0
    for index, token in enumerate(tokens[1:], 1):
        if token.text in ("(", "[", "{"):
            depth += 1
        elif token.text in (")", "]", "}"):
            depth -= 1
        if depth == 0 and _upper(token) in {
            "DEFAULT",
            "MATERIALIZED",
            "ALIAS",
            "CODEC",
            "COMMENT",
            "TTL",
        }:
            modifiers.append((_upper(token), index))
    type_end = modifiers[0][1] if modifiers else len(tokens)
    column_type = text[tokens[1].start : tokens[type_end - 1].end]
    values = {}
    for offset, (kind, index) in enumerate(modifiers):
        end = modifiers[offset + 1][1] if offset + 1 < len(modifiers) else len(tokens)
        values[kind] = text[tokens[index + 1].start : tokens[end - 1].end]
    kind = next((item for item in ("MATERIALIZED", "ALIAS", "DEFAULT") if item in values), None)
    return ColumnDefinition(
        name, column_type, kind, values.get(kind), values.get("CODEC"), values.get("COMMENT")
    )


def _clauses(text: str, tokens: tuple[_Token, ...], start: int) -> dict[str, str]:
    starts: list[tuple[str, int, int]] = []
    position = start
    while position < len(tokens):
        word = _upper(tokens[position])
        if word not in _CLAUSES:
            raise ValueError(f"unsupported CREATE TABLE clause: {tokens[position].text}")
        next_position = position + (2 if word in {"PARTITION", "PRIMARY", "ORDER", "SAMPLE"} else 1)
        expected = "KEY" if word == "PRIMARY" else "BY"
        if next_position > len(tokens) or (
            next_position == position + 2 and _upper(tokens[position + 1]) != expected
        ):
            raise ValueError(f"invalid {word} clause")
        if any(name == word for name, _, _ in starts):
            raise ValueError(f"duplicate {word} clause")
        starts.append((word, position, next_position))
        position = next_position
        depth = 0
        while position < len(tokens):
            token = tokens[position]
            if token.text == "(":
                depth += 1
            elif token.text == ")":
                depth -= 1
            if depth == 0 and _upper(token) in _CLAUSES and _clause_start(tokens, position):
                break
            position += 1
        if depth or position == next_position:
            raise ValueError(f"unbalanced or empty {word} clause")
    clauses: dict[str, str] = {}
    for index, (word, _, value_start) in enumerate(starts):
        stop = starts[index + 1][1] if index + 1 < len(starts) else len(tokens)
        clauses[word] = text[tokens[value_start].start : tokens[stop - 1].end]
    if "ENGINE" not in clauses or "ORDER" not in clauses:
        raise ValueError("rebuild requires ENGINE and ORDER BY")
    return clauses


def _clause_start(tokens: tuple[_Token, ...], index: int) -> bool:
    word = _upper(tokens[index])
    return word not in {"PARTITION", "PRIMARY", "ORDER", "SAMPLE"} or (
        index + 1 < len(tokens)
        and _upper(tokens[index + 1]) == ("KEY" if word == "PRIMARY" else "BY")
    )


def _engine(
    text: str, tokens: tuple[_Token, ...], start: int
) -> tuple[int, int, str, tuple[str, ...] | None]:
    depth = 0
    for index in range(start, len(tokens)):
        token = tokens[index]
        if token.text == "(":
            depth += 1
        elif token.text == ")":
            depth -= 1
        if depth or _upper(token) != "ENGINE":
            continue
        if index + 2 >= len(tokens) or tokens[index + 1].text != "=":
            raise ValueError("ENGINE must use explicit ENGINE = name syntax")
        name = _ident(tokens[index + 2])
        if not name:
            raise ValueError("invalid ENGINE name")
        end = index + 2
        args = None
        if end + 1 < len(tokens) and tokens[end + 1].text == "(":
            close = _closing(tokens, end + 1)
            args = (
                tuple(
                    text[tokens[a].start : tokens[b - 1].end]
                    for a, b in _split(tokens, end + 2, close)
                )
                if close > end + 2
                else ()
            )
            end = close
        return tokens[index].start, tokens[end].end, name, args
    raise ValueError("missing ENGINE clause")


def _helper_engine(parsed: _DDL, role: str, uuid: str) -> str:
    name = parsed.engine_name
    args = parsed.engine_args or ()
    if name.lower().startswith(("replicated", "shared")):
        if args and _string(args[0]) and (len(args) < 2 or not _string(args[1])):
            raise ValueError("ambiguous replicated Keeper path/replica arguments")
        if len(args) >= 2 and _string(args[0]):
            original = (
                _unquote(args[0])
                .replace("{database}", _ident(parsed.tokens[parsed.name_at]))
                .replace("{table}", _ident(parsed.tokens[parsed.name_at + 2]))
            )
            # Sibling roots survive removal of the old table's Keeper subtree.
            # Freeze name-dependent macros before the eventual EXCHANGE/RENAME.
            parent = original.rstrip("/").rsplit("/", 1)[0]
            path = parent + f"/__chm_{role}_{uuid}"
            for macro in dict.fromkeys(re.findall(r"\{[A-Za-z_][A-Za-z_0-9]*\}", original)):
                if macro not in path:
                    path += "/" + macro  # Retain shard/UUID discrimination from the old leaf.
            replica = args[1]
            args = ("'" + path.replace("'", "\\'") + "'", replica, *args[2:])
        else:
            args = (f"'/clickhouse/tables/{uuid}/{{shard}}'", "'{replica}'", *args)
    return (
        f"ENGINE = {name}({', '.join(args)})"
        if args or parsed.engine_args is not None
        else f"ENGINE = {name}"
    )


def _insert_cluster(sql: str, cluster: str) -> str:
    tokens = _tokens(sql)
    offset = tokens[6].end if len(tokens) > 6 and _upper(tokens[5]) == "UUID" else tokens[4].end
    return sql[:offset] + f" ON CLUSTER {_quote(cluster)}" + sql[offset:]


def _projection(select: str | None, columns: tuple[str, ...]) -> str:
    if select is None:
        return ", ".join(_quote(column) for column in columns)
    tokens = _tokens(select)
    forbidden = {
        "SELECT",
        "FROM",
        "JOIN",
        "WHERE",
        "UNION",
        "INTO",
        "PREWHERE",
        "GROUP",
        "HAVING",
        "LIMIT",
    }
    if not tokens or any(_upper(token) in forbidden or token.text == ";" for token in tokens):
        raise ValueError("select must be a projection expression list, not a full query")
    expressions = _split(tokens, 0, len(tokens))
    if len(expressions) != len(columns):
        raise ValueError("projection expression count must match insertable target columns")
    projected = []
    for (first, last), column in zip(expressions, columns):
        end = last
        if last - first >= 3 and _upper(tokens[last - 2]) == "AS":
            if _ident(tokens[last - 1]) != column:
                raise ValueError("projection alias must equal the corresponding target column")
            end = last - 2
        expr = select[tokens[first].start : tokens[end - 1].end]
        projected.append(f"({expr}) AS {_quote(column)}")
    return ", ".join(projected)


def _order_columns(expression: str) -> list[str]:
    tokens = _tokens(expression)
    if tokens and tokens[0].text == "(" and _closing(tokens, 0) == len(tokens) - 1:
        tokens = tokens[1:-1]
    return (
        [expression[tokens[a].start : tokens[b - 1].end] for a, b in _split(tokens, 0, len(tokens))]
        if tokens
        else []
    )


def _settings(expression: str) -> dict[str, str]:
    tokens = _tokens(expression)
    if not tokens:
        return {}
    result = {}
    for first, last in _split(tokens, 0, len(tokens)):
        if last - first < 3 or tokens[first + 1].text != "=":
            raise ValueError("invalid ENGINE settings")
        result[_ident(tokens[first])] = expression[tokens[first + 2].start : tokens[last - 1].end]
    return result


def _engine_text(parsed: _DDL) -> str:
    return parsed.sql[parsed.engine_at : parsed.engine_end].split("=", 1)[1].strip()


def _replace(sql: str, replacements: list[tuple[int, int, str]]) -> str:
    for start, end, replacement in sorted(replacements, reverse=True):
        sql = sql[:start] + replacement + sql[end:]
    return sql


def _ident(token: _Token) -> str:
    text = token.text
    if text.startswith(("`", '"')) and text.endswith(text[0]):
        return text[1:-1].replace(text[0] * 2, text[0])
    return text if _IDENTIFIER.fullmatch(text) else ""


def _upper(token: _Token) -> str:
    return token.text.upper() if _IDENTIFIER.fullmatch(token.text) else ""


def _string(text: str) -> bool:
    return len(text) >= 2 and text[0] == "'" and text[-1] == "'"


def _unquote(text: str) -> str:
    return text[1:-1].replace("\\'", "'").replace("''", "'")


def _quote(identifier: str) -> str:
    if not identifier or "\x00" in identifier:
        raise ValueError("invalid SQL identifier")
    return "`" + identifier.replace("`", "``") + "`"


def _validate_uuid(uuid: str) -> None:
    if not re.fullmatch(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}", uuid):
        raise ValueError("helper UUID must be a canonical UUID")
