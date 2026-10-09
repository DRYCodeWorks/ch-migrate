"""ClickHouse schema introspection: structured DDL parsing and live schema capture."""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

# Alembic's bookkeeping table: not part of the user's schema, so snapshot, diff
# and deps leave it out.
VERSION_TABLE = "alembic_version"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class ColumnDefinition:
    name: str
    type: str
    default_kind: str | None = None  # DEFAULT, MATERIALIZED, ALIAS
    default_expr: str | None = None
    codec: str | None = None
    comment: str | None = None


@dataclass
class TableDefinition:
    name: str
    engine: str
    columns: list[ColumnDefinition] = field(default_factory=list)
    order_by: list[str] = field(default_factory=list)
    partition_by: str | None = None
    ttl: str | None = None
    settings: dict[str, str] = field(default_factory=dict)
    raw_ddl: str = ""


@dataclass
class ViewDefinition:
    name: str
    select_query: str
    raw_ddl: str = ""


@dataclass
class MVDefinition:
    name: str
    target_table: str | None = None
    source_tables: list[str] = field(default_factory=list)
    select_query: str = ""
    engine: str | None = None
    raw_ddl: str = ""


@dataclass
class DictDefinition:
    name: str
    primary_key: str | None = None
    source_type: str | None = None  # e.g. "clickhouse", "mysql", "http"
    source_table: str | None = None
    source_db: str | None = None
    source_query: str | None = None
    layout: str | None = None
    lifetime: str | None = None
    structure_keys: list[str] = field(default_factory=list)
    structure_attributes: list[str] = field(default_factory=list)
    raw_ddl: str = ""


@dataclass
class Schema:
    tables: dict[str, TableDefinition] = field(default_factory=dict)
    views: dict[str, ViewDefinition] = field(default_factory=dict)
    materialized_views: dict[str, MVDefinition] = field(default_factory=dict)
    dictionaries: dict[str, DictDefinition] = field(default_factory=dict)
    database: str = ""
    ch_version: str | None = None


# ---------------------------------------------------------------------------
# Dependency graph
# ---------------------------------------------------------------------------


class DepType(str, Enum):
    SCHEMA = "schema"
    DATA_FLOW = "data_flow"


@dataclass
class DependencyEdge:
    source: str
    target: str
    dep_type: DepType


@dataclass
class ObjectNode:
    name: str
    obj_type: Literal["table", "view", "materialized_view", "dictionary"]


@dataclass
class DependencyGraph:
    nodes: dict[str, ObjectNode] = field(default_factory=dict)
    edges: list[DependencyEdge] = field(default_factory=list)

    def topological_order(self) -> list[str]:
        """Return names in safe drop/recreate order (leaves first)."""
        in_degree: dict[str, int] = {name: 0 for name in self.nodes}
        adj: dict[str, list[str]] = {name: [] for name in self.nodes}
        for edge in self.edges:
            if edge.target in in_degree and edge.source in adj:
                adj[edge.source].append(edge.target)
                in_degree[edge.target] += 1

        queue = deque(n for n, d in in_degree.items() if d == 0)
        result: list[str] = []
        while queue:
            node = queue.popleft()
            result.append(node)
            for neighbor in adj.get(node, []):
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        # Append any remaining nodes (cycles) at the end
        for name in self.nodes:
            if name not in result:
                result.append(name)

        return result

    def affected_by_drop(self, name: str) -> list[ObjectNode]:
        """Return direct dependents that would be affected by dropping the given object."""
        seen: set[str] = set()
        affected: list[ObjectNode] = []
        for edge in self.edges:
            if edge.source == name and edge.target in self.nodes and edge.target not in seen:
                seen.add(edge.target)
                affected.append(self.nodes[edge.target])
        return affected


# ---------------------------------------------------------------------------
# DDL parsing
# ---------------------------------------------------------------------------

# Regex patterns for parsing CREATE statements
_RE_CREATE_TABLE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(?:`?(\w+)`?\.)?`?(\w+)`?"
    r"\s*(?:ON\s+CLUSTER\s+\S+\s*)?\(",
    re.IGNORECASE,
)

_RE_ENGINE = re.compile(r"ENGINE\s*=\s*(\w+(?:\(.*?\))?)", re.IGNORECASE)

_RE_ORDER_BY = re.compile(r"ORDER\s+BY\s+(.+?)(?=\s*(?:PARTITION|TTL|SETTINGS|$))", re.IGNORECASE)

_RE_PARTITION_BY = re.compile(
    r"PARTITION\s+BY\s+(.+?)(?=\s*(?:ORDER|TTL|SETTINGS|$))", re.IGNORECASE
)

_RE_TTL = re.compile(r"TTL\s+(.+?)(?=\s*(?:SETTINGS|$))", re.IGNORECASE)

_RE_SETTINGS = re.compile(r"SETTINGS\s+(.+)$", re.IGNORECASE | re.MULTILINE)

_RE_CREATE_VIEW = re.compile(
    r"CREATE\s+VIEW\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(?:`?(\w+)`?\.)?`?(\w+)`?"
    r"\s+AS\s+",
    re.IGNORECASE,
)

_RE_CREATE_MV = re.compile(
    r"CREATE\s+MATERIALIZED\s+VIEW\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(?:`?(\w+)`?\.)?`?(\w+)`?"
    r"\s*(?:ON\s+CLUSTER\s+\S+\s*)?",
    re.IGNORECASE,
)

_RE_MV_TO = re.compile(r"TO\s+(?:`?(\w+)`?\.)?`?(\w+)`?", re.IGNORECASE)

_RE_CREATE_DICT = re.compile(
    r"CREATE\s+(?:OR\s+REPLACE\s+)?DICTIONARY\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(?:`?(\w+)`?\.)?`?(\w+)`?"
    r"\s*(?:ON\s+CLUSTER\s+\S+\s*)?\(",
    re.IGNORECASE,
)


def _parse_columns(columns_block: str) -> list[ColumnDefinition]:
    """Parse column definitions from the block between CREATE TABLE (...).

    Handles nested parentheses in types like Nullable(String), Tuple(a UInt8, b String).
    """
    columns: list[ColumnDefinition] = []
    depth = 0
    current: list[str] = []

    for char in columns_block:
        if char == "(":
            depth += 1
            current.append(char)
        elif char == ")":
            depth -= 1
            current.append(char)
        elif char == "," and depth == 0:
            line = "".join(current).strip()
            if line:
                col = _parse_single_column(line)
                if col:
                    columns.append(col)
            current = []
        else:
            current.append(char)

    # Last column (no trailing comma)
    line = "".join(current).strip()
    if line:
        col = _parse_single_column(line)
        if col:
            columns.append(col)

    return columns


def _parse_single_column(line: str) -> ColumnDefinition | None:
    """Parse a single column definition line."""
    line = line.strip()
    if not line:
        return None

    # Skip constraints (INDEX, PROJECTION, CONSTRAINT)
    if re.match(r"(?:INDEX|PROJECTION|CONSTRAINT)\s+", line, re.IGNORECASE):
        return None

    # Match: `name` Type [DEFAULT|MATERIALIZED|ALIAS expr] [CODEC(...)] [COMMENT '...']
    m = re.match(r"`?(\w+)`?\s+(.+)", line)
    if not m:
        return None

    name = m.group(1)
    rest = m.group(2)

    # Extract COMMENT
    comment = None
    comment_match = re.search(r"COMMENT\s+'((?:[^'\\]|\\.)*)'", rest, re.IGNORECASE)
    if comment_match:
        comment = comment_match.group(1)
        rest = rest[: comment_match.start()].rstrip()

    # Extract CODEC
    codec = None
    codec_match = re.search(r"CODEC\s*\((.+?)\)\s*$", rest, re.IGNORECASE)
    if codec_match:
        codec = codec_match.group(1)
        rest = rest[: codec_match.start()].rstrip()

    # Extract DEFAULT/MATERIALIZED/ALIAS
    default_kind = None
    default_expr = None
    default_match = re.search(
        r"\b(DEFAULT|MATERIALIZED|ALIAS)\s+(.+)$", rest, re.IGNORECASE
    )
    if default_match:
        default_kind = default_match.group(1).upper()
        default_expr = default_match.group(2).strip()
        rest = rest[: default_match.start()].rstrip()

    col_type = rest.strip()

    return ColumnDefinition(
        name=name,
        type=col_type,
        default_kind=default_kind,
        default_expr=default_expr,
        codec=codec,
        comment=comment,
    )


def _extract_columns_block(ddl: str, start_paren_pos: int) -> str:
    """Extract the columns block from CREATE TABLE, handling nested parens."""
    depth = 0
    i = start_paren_pos
    while i < len(ddl):
        if ddl[i] == "(":
            depth += 1
        elif ddl[i] == ")":
            depth -= 1
            if depth == 0:
                return ddl[start_paren_pos + 1 : i]
        i += 1
    return ddl[start_paren_pos + 1 :]


def _parse_order_by(expr: str) -> list[str]:
    """Parse ORDER BY expression into a list of key expressions."""
    expr = expr.strip()
    # Handle tuple syntax: (col1, col2, col3)
    if expr.startswith("("):
        expr = expr.strip("()")
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for char in expr:
        if char == "(":
            depth += 1
            current.append(char)
        elif char == ")":
            depth -= 1
            current.append(char)
        elif char == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    remaining = "".join(current).strip()
    if remaining:
        parts.append(remaining)
    return parts


def _parse_settings(settings_str: str) -> dict[str, str]:
    """Parse SETTINGS key=value pairs."""
    result: dict[str, str] = {}
    for pair in settings_str.split(","):
        pair = pair.strip()
        if "=" in pair:
            k, v = pair.split("=", 1)
            result[k.strip()] = v.strip()
    return result


def _extract_from_tables(select_query: str) -> list[str]:
    """Extract table references from a SELECT query (FROM and JOIN clauses)."""
    tables: list[str] = []
    # Match FROM db.table or FROM table (not subqueries or function calls)
    for m in re.finditer(
        r"(?:FROM|JOIN)\s+(?:`?(\w+)`?\.)?`?(\w+)`?(?!\s*\()", select_query, re.IGNORECASE
    ):
        db_part = m.group(1)
        table_name = m.group(2)
        # Skip system tables and subquery keywords
        if table_name.upper() in ("SELECT", "LATERAL", "EACH"):
            continue
        full_name = f"{db_part}.{table_name}" if db_part else table_name
        if full_name not in tables:
            tables.append(full_name)
    return tables


def parse_create_table(ddl: str) -> TableDefinition | None:
    """Parse a CREATE TABLE statement into a TableDefinition."""
    m = _RE_CREATE_TABLE.search(ddl)
    if not m:
        return None

    name = m.group(2)
    paren_pos = ddl.index("(", m.end() - 1)
    columns_block = _extract_columns_block(ddl, paren_pos)
    columns = _parse_columns(columns_block)

    # Everything after the closing paren of columns
    after_columns = ddl[paren_pos + len(columns_block) + 2 :]

    engine_match = _RE_ENGINE.search(after_columns)
    engine = engine_match.group(1) if engine_match else ""

    order_by: list[str] = []
    ob_match = _RE_ORDER_BY.search(after_columns)
    if ob_match:
        order_by = _parse_order_by(ob_match.group(1))

    partition_by = None
    pb_match = _RE_PARTITION_BY.search(after_columns)
    if pb_match:
        partition_by = pb_match.group(1).strip()

    ttl = None
    ttl_match = _RE_TTL.search(after_columns)
    if ttl_match:
        ttl = ttl_match.group(1).strip()

    settings: dict[str, str] = {}
    settings_match = _RE_SETTINGS.search(after_columns)
    if settings_match:
        settings = _parse_settings(settings_match.group(1))

    return TableDefinition(
        name=name,
        engine=engine,
        columns=columns,
        order_by=order_by,
        partition_by=partition_by,
        ttl=ttl,
        settings=settings,
        raw_ddl=ddl,
    )


def parse_create_view(ddl: str) -> ViewDefinition | None:
    """Parse a CREATE VIEW statement into a ViewDefinition."""
    m = _RE_CREATE_VIEW.search(ddl)
    if not m:
        return None

    name = m.group(2)
    select_query = ddl[m.end() :].strip()

    return ViewDefinition(name=name, select_query=select_query, raw_ddl=ddl)


def parse_create_mv(ddl: str) -> MVDefinition | None:
    """Parse a CREATE MATERIALIZED VIEW statement into an MVDefinition."""
    m = _RE_CREATE_MV.search(ddl)
    if not m:
        return None

    name = m.group(2)
    rest = ddl[m.end() :]

    # Check for TO clause (target table)
    target_table = None
    to_match = _RE_MV_TO.search(rest)
    if to_match:
        target_db = to_match.group(1)
        target_tbl = to_match.group(2)
        target_table = f"{target_db}.{target_tbl}" if target_db else target_tbl

    # Find the AS SELECT part
    as_match = re.search(r"\bAS\s+(?=SELECT\b)", rest, re.IGNORECASE)
    select_query = ""
    if as_match:
        select_query = rest[as_match.end() :].strip()

    # Extract engine if present (between TO and AS, or before AS)
    engine = None
    engine_match = re.search(r"ENGINE\s*=\s*(\w+(?:\([^)]*\))?)", rest, re.IGNORECASE)
    if engine_match:
        engine = engine_match.group(1)

    source_tables = _extract_from_tables(select_query) if select_query else []

    return MVDefinition(
        name=name,
        target_table=target_table,
        source_tables=source_tables,
        select_query=select_query,
        engine=engine,
        raw_ddl=ddl,
    )


def parse_create_dictionary(ddl: str) -> DictDefinition | None:
    """Parse a CREATE DICTIONARY statement into a DictDefinition."""
    m = _RE_CREATE_DICT.search(ddl)
    if not m:
        return None

    name = m.group(2)

    # Extract PRIMARY KEY
    pk_match = re.search(r"PRIMARY\s+KEY\s+(\w+)", ddl, re.IGNORECASE)
    primary_key = pk_match.group(1) if pk_match else None

    # Extract SOURCE
    source_type = None
    source_table = None
    source_db = None
    source_query = None
    source_match = re.search(r"SOURCE\s*\(\s*(\w+)\s*\(", ddl, re.IGNORECASE)
    if source_match:
        source_type = source_match.group(1).lower()

        # Extract table from SOURCE(CLICKHOUSE(TABLE '...' DB '...'))
        tbl_match = re.search(
            r"TABLE\s+'([^']+)'", ddl[source_match.start() :], re.IGNORECASE
        )
        if tbl_match:
            source_table = tbl_match.group(1)

        db_match = re.search(
            r"DB\s+'([^']+)'", ddl[source_match.start() :], re.IGNORECASE
        )
        if db_match:
            source_db = db_match.group(1)

        query_match = re.search(
            r"QUERY\s+'((?:[^'\\]|\\.)*)'",
            ddl[source_match.start() :],
            re.IGNORECASE,
        )
        if query_match:
            source_query = query_match.group(1)

    # Extract LAYOUT
    layout_match = re.search(r"LAYOUT\s*\(\s*(\w+)", ddl, re.IGNORECASE)
    layout = layout_match.group(1) if layout_match else None

    # Extract LIFETIME
    lifetime_match = re.search(r"LIFETIME\s*\((.+?)\)", ddl, re.IGNORECASE)
    lifetime = lifetime_match.group(1).strip() if lifetime_match else None

    return DictDefinition(
        name=name,
        primary_key=primary_key,
        source_type=source_type,
        source_table=source_table,
        source_db=source_db,
        source_query=source_query,
        layout=layout,
        lifetime=lifetime,
        raw_ddl=ddl,
    )


def parse_create_statement(
    ddl: str,
) -> TableDefinition | ViewDefinition | MVDefinition | DictDefinition | None:
    """Parse any CREATE statement into the appropriate definition type.

    Returns None if the DDL cannot be parsed.
    """
    ddl_stripped = ddl.strip()

    if re.match(r"CREATE\s+MATERIALIZED\s+VIEW\b", ddl_stripped, re.IGNORECASE):
        return parse_create_mv(ddl_stripped)
    if re.match(r"CREATE\s+(?:OR\s+REPLACE\s+)?DICTIONARY\b", ddl_stripped, re.IGNORECASE):
        return parse_create_dictionary(ddl_stripped)
    if re.match(r"CREATE\s+VIEW\b", ddl_stripped, re.IGNORECASE):
        return parse_create_view(ddl_stripped)
    if re.match(r"CREATE\s+TABLE\b", ddl_stripped, re.IGNORECASE):
        return parse_create_table(ddl_stripped)

    return None


# ---------------------------------------------------------------------------
# Live schema introspection
# ---------------------------------------------------------------------------


def list_objects(
    client: Any, database: str, obj_type: str = "table"
) -> list[str]:
    """List objects of a given type in a database.

    Args:
        client: clickhouse-connect client.
        database: Database name.
        obj_type: One of 'table', 'view', 'materialized_view', 'dictionary'.

    Returns:
        List of object names.
    """
    if obj_type == "dictionary":
        result = client.query(
            "SELECT name FROM system.dictionaries WHERE database = {db:String}",
            parameters={"db": database},
        )
        return [row[0] for row in result.result_rows]

    engine_filter = {
        "table": f"engine NOT IN ('View', 'MaterializedView') AND name != '{VERSION_TABLE}'",
        "view": "engine = 'View'",
        "materialized_view": "engine = 'MaterializedView'",
    }.get(obj_type, "1=1")

    result = client.query(
        f"SELECT name FROM system.tables "
        f"WHERE database = {{db:String}} AND {engine_filter}",
        parameters={"db": database},
    )
    return [row[0] for row in result.result_rows]


def get_create_statement(
    client: Any, database: str, name: str, obj_type: str = "table"
) -> str:
    """Get the CREATE statement for an object.

    Args:
        client: clickhouse-connect client.
        database: Database name.
        name: Object name.
        obj_type: One of 'table', 'view', 'materialized_view', 'dictionary'.

    Returns:
        The CREATE statement as a string.
    """
    if obj_type == "dictionary":
        show_type = "DICTIONARY"
    else:
        show_type = "TABLE"

    result = client.query(f"SHOW CREATE {show_type} `{database}`.`{name}`")
    if result.result_rows:
        return result.result_rows[0][0]
    return ""


def get_live_schema(client: Any, database: str) -> Schema:
    """Capture the full schema from a live ClickHouse database.

    Args:
        client: clickhouse-connect client.
        database: Database name.

    Returns:
        A Schema object with all tables, views, MVs, and dictionaries.
    """
    # Detect CH version
    version_result = client.query("SELECT version()")
    ch_version = version_result.result_rows[0][0] if version_result.result_rows else None

    schema = Schema(database=database, ch_version=ch_version)

    # Tables
    for name in list_objects(client, database, "table"):
        ddl = get_create_statement(client, database, name, "table")
        parsed = parse_create_table(ddl)
        if parsed:
            schema.tables[name] = parsed
        else:
            # Fallback: store raw DDL in a minimal TableDefinition
            schema.tables[name] = TableDefinition(name=name, engine="", raw_ddl=ddl)

    # Views
    for name in list_objects(client, database, "view"):
        ddl = get_create_statement(client, database, name, "table")
        parsed = parse_create_view(ddl)
        if parsed:
            schema.views[name] = parsed
        else:
            schema.views[name] = ViewDefinition(name=name, select_query="", raw_ddl=ddl)

    # Materialized views
    for name in list_objects(client, database, "materialized_view"):
        ddl = get_create_statement(client, database, name, "table")
        parsed = parse_create_mv(ddl)
        if parsed:
            schema.materialized_views[name] = parsed
        else:
            schema.materialized_views[name] = MVDefinition(name=name, raw_ddl=ddl)

    # Dictionaries
    for name in list_objects(client, database, "dictionary"):
        ddl = get_create_statement(client, database, name, "dictionary")
        parsed = parse_create_dictionary(ddl)
        if parsed:
            schema.dictionaries[name] = parsed
        else:
            schema.dictionaries[name] = DictDefinition(name=name, raw_ddl=ddl)

    return schema


def get_dependencies(client: Any, database: str) -> DependencyGraph:
    """Build a dependency graph from the live database.

    Queries system.tables for materialized views and system.dictionaries for
    dictionary source tables. Edges are typed as schema or data_flow.

    Args:
        client: clickhouse-connect client.
        database: Database name.

    Returns:
        A DependencyGraph with nodes and typed edges.
    """
    graph = DependencyGraph()
    schema = get_live_schema(client, database)

    # Add all objects as nodes
    for name in schema.tables:
        graph.nodes[name] = ObjectNode(name=name, obj_type="table")
    for name in schema.views:
        graph.nodes[name] = ObjectNode(name=name, obj_type="view")
    for name in schema.materialized_views:
        graph.nodes[name] = ObjectNode(name=name, obj_type="materialized_view")
    for name in schema.dictionaries:
        graph.nodes[name] = ObjectNode(name=name, obj_type="dictionary")

    # MV dependencies
    for name, mv in schema.materialized_views.items():
        for src in mv.source_tables:
            # Normalize: strip db prefix if it matches current database
            src_name = src.split(".")[-1] if "." in src else src
            if src_name in graph.nodes:
                # Data flow: MV triggers on INSERT to source
                graph.edges.append(
                    DependencyEdge(source=src_name, target=name, dep_type=DepType.DATA_FLOW)
                )
            # Schema dependency: MV SELECT references this table
            graph.edges.append(
                DependencyEdge(source=src_name, target=name, dep_type=DepType.SCHEMA)
            )

        # TO table dependency
        if mv.target_table:
            tgt_name = mv.target_table.split(".")[-1] if "." in mv.target_table else mv.target_table
            if tgt_name in graph.nodes:
                graph.edges.append(
                    DependencyEdge(source=name, target=tgt_name, dep_type=DepType.DATA_FLOW)
                )

    # Dictionary dependencies
    for name, d in schema.dictionaries.items():
        if d.source_table:
            src_name = d.source_table
            if src_name in graph.nodes:
                graph.edges.append(
                    DependencyEdge(source=src_name, target=name, dep_type=DepType.SCHEMA)
                )
        elif d.source_query:
            # Parse tables from the source query
            for src in _extract_from_tables(d.source_query):
                src_name = src.split(".")[-1] if "." in src else src
                if src_name in graph.nodes:
                    graph.edges.append(
                        DependencyEdge(source=src_name, target=name, dep_type=DepType.SCHEMA)
                    )

    return graph
