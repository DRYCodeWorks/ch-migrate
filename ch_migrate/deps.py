"""Dependency graph analysis and migration validation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ch_migrate.introspect import (
    DependencyGraph,
    DepType,
    ObjectNode,
    get_dependencies,
)


@dataclass
class MigrationWarning:
    severity: str  # "error" or "warning"
    message: str
    affected_objects: list[str]


# Patterns that detect destructive operations
_RE_DROP_TABLE = re.compile(
    r"DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:`?(\w+)`?\.)?`?(\w+)`?",
    re.IGNORECASE,
)

_RE_DROP_VIEW = re.compile(
    r"DROP\s+(?:MATERIALIZED\s+)?VIEW\s+(?:IF\s+EXISTS\s+)?(?:`?(\w+)`?\.)?`?(\w+)`?",
    re.IGNORECASE,
)

_RE_DROP_DICT = re.compile(
    r"DROP\s+DICTIONARY\s+(?:IF\s+EXISTS\s+)?(?:`?(\w+)`?\.)?`?(\w+)`?",
    re.IGNORECASE,
)


def validate_migration(sql: str, graph: DependencyGraph) -> list[MigrationWarning]:
    """Check if migration SQL would break dependencies in the graph.

    Args:
        sql: The migration SQL to validate.
        graph: A live DependencyGraph from introspect.get_dependencies().

    Returns:
        List of warnings about potential dependency breakage.
    """
    warnings: list[MigrationWarning] = []

    # Check DROP TABLE
    for m in _RE_DROP_TABLE.finditer(sql):
        table_name = m.group(2)
        if table_name in graph.nodes:
            affected = graph.affected_by_drop(table_name)
            if affected:
                # Distinguish schema vs data_flow impact
                schema_deps = []
                data_flow_deps = []
                for edge in graph.edges:
                    if edge.source == table_name:
                        if edge.dep_type == DepType.SCHEMA:
                            schema_deps.append(edge.target)
                        elif edge.dep_type == DepType.DATA_FLOW:
                            data_flow_deps.append(edge.target)

                if schema_deps:
                    warnings.append(MigrationWarning(
                        severity="error",
                        message=f"DROP TABLE {table_name} breaks schema dependencies: {', '.join(schema_deps)}",
                        affected_objects=schema_deps,
                    ))
                if data_flow_deps:
                    warnings.append(MigrationWarning(
                        severity="warning",
                        message=f"DROP TABLE {table_name} breaks data flow to: {', '.join(data_flow_deps)}",
                        affected_objects=data_flow_deps,
                    ))

    # Check DROP VIEW / DROP MATERIALIZED VIEW
    for m in _RE_DROP_VIEW.finditer(sql):
        view_name = m.group(2)
        if view_name in graph.nodes:
            affected = graph.affected_by_drop(view_name)
            if affected:
                affected_names = [n.name for n in affected]
                warnings.append(MigrationWarning(
                    severity="warning",
                    message=f"DROP VIEW {view_name} affects: {', '.join(affected_names)}",
                    affected_objects=affected_names,
                ))

    # Check DROP DICTIONARY
    for m in _RE_DROP_DICT.finditer(sql):
        dict_name = m.group(2)
        if dict_name in graph.nodes:
            affected = graph.affected_by_drop(dict_name)
            if affected:
                affected_names = [n.name for n in affected]
                warnings.append(MigrationWarning(
                    severity="warning",
                    message=f"DROP DICTIONARY {dict_name} affects: {', '.join(affected_names)}",
                    affected_objects=affected_names,
                ))

    return warnings


def build_dependency_graph(client: Any, database: str) -> DependencyGraph:
    """Build a dependency graph from the live database.

    Convenience wrapper around introspect.get_dependencies().

    Args:
        client: clickhouse-connect client.
        database: Database name.

    Returns:
        A DependencyGraph with nodes and typed edges.
    """
    return get_dependencies(client, database)
