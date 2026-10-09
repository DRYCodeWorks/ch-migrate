"""Schema comparison: field-by-field structured diff between two Schema objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

from ch_migrate.introspect import (
    ColumnDefinition,
    Schema,
    TableDefinition,
)


class DiffStatus(str, Enum):
    IN_SYNC = "in_sync"
    MODIFIED = "modified"
    LOCAL_ONLY = "local_only"
    REMOTE_ONLY = "remote_only"


@dataclass
class FieldDiff:
    field_name: str
    local_value: str | None
    remote_value: str | None
    message: str


@dataclass
class SchemaDiff:
    name: str
    obj_type: str  # "table", "view", "materialized_view", "dictionary"
    status: DiffStatus
    field_diffs: list[FieldDiff] = field(default_factory=list)


def _compare_columns(
    local_cols: list[ColumnDefinition],
    remote_cols: list[ColumnDefinition],
) -> list[FieldDiff]:
    """Compare column lists field-by-field."""
    diffs: list[FieldDiff] = []
    local_map = {c.name: c for c in local_cols}
    remote_map = {c.name: c for c in remote_cols}

    all_names = dict.fromkeys([c.name for c in local_cols] + [c.name for c in remote_cols])

    for name in all_names:
        local_col = local_map.get(name)
        remote_col = remote_map.get(name)

        if local_col and not remote_col:
            diffs.append(FieldDiff(
                field_name=f"column '{name}'",
                local_value=local_col.type,
                remote_value=None,
                message=f"column '{name}' {local_col.type} is in the snapshot but not in the database",
            ))
        elif remote_col and not local_col:
            diffs.append(FieldDiff(
                field_name=f"column '{name}'",
                local_value=None,
                remote_value=remote_col.type,
                message=f"column '{name}' {remote_col.type} is in the database but not in the snapshot",
            ))
        elif local_col and remote_col:
            if local_col.type != remote_col.type:
                diffs.append(FieldDiff(
                    field_name=f"column '{name}' type",
                    local_value=local_col.type,
                    remote_value=remote_col.type,
                    message=f"column '{name}' type differs: {local_col.type} vs {remote_col.type}",
                ))
            if local_col.default_kind != remote_col.default_kind:
                diffs.append(FieldDiff(
                    field_name=f"column '{name}' default_kind",
                    local_value=local_col.default_kind,
                    remote_value=remote_col.default_kind,
                    message=f"column '{name}' default kind differs",
                ))
            if local_col.default_expr != remote_col.default_expr:
                diffs.append(FieldDiff(
                    field_name=f"column '{name}' default_expr",
                    local_value=local_col.default_expr,
                    remote_value=remote_col.default_expr,
                    message=f"column '{name}' default expression differs",
                ))
            if local_col.codec != remote_col.codec:
                diffs.append(FieldDiff(
                    field_name=f"column '{name}' codec",
                    local_value=local_col.codec,
                    remote_value=remote_col.codec,
                    message=f"column '{name}' codec differs: {local_col.codec} vs {remote_col.codec}",
                ))

    return diffs


def _compare_tables(local: TableDefinition, remote: TableDefinition) -> list[FieldDiff]:
    """Field-by-field comparison of two TableDefinitions."""
    diffs: list[FieldDiff] = []

    # Engine
    if local.engine != remote.engine:
        diffs.append(FieldDiff(
            field_name="engine",
            local_value=local.engine,
            remote_value=remote.engine,
            message=f"engine differs: {local.engine} vs {remote.engine}",
        ))

    # Columns
    diffs.extend(_compare_columns(local.columns, remote.columns))

    # ORDER BY
    if local.order_by != remote.order_by:
        diffs.append(FieldDiff(
            field_name="order_by",
            local_value=", ".join(local.order_by),
            remote_value=", ".join(remote.order_by),
            message=f"ORDER BY differs",
        ))

    # PARTITION BY
    if local.partition_by != remote.partition_by:
        diffs.append(FieldDiff(
            field_name="partition_by",
            local_value=local.partition_by,
            remote_value=remote.partition_by,
            message=f"PARTITION BY differs",
        ))

    # TTL
    if local.ttl != remote.ttl:
        diffs.append(FieldDiff(
            field_name="ttl",
            local_value=local.ttl,
            remote_value=remote.ttl,
            message=f"TTL differs",
        ))

    # Settings
    if local.settings != remote.settings:
        diffs.append(FieldDiff(
            field_name="settings",
            local_value=str(local.settings),
            remote_value=str(remote.settings),
            message=f"SETTINGS differ",
        ))

    return diffs


def _normalize_ddl(raw: str) -> str:
    """Normalize raw DDL for fallback string comparison."""
    import re
    s = raw.strip()
    s = re.sub(r"\s+", " ", s)
    return s


def _compare_raw_ddl(local_ddl: str, remote_ddl: str) -> list[FieldDiff]:
    """Fallback: normalized string comparison for unparseable objects."""
    if _normalize_ddl(local_ddl) != _normalize_ddl(remote_ddl):
        return [FieldDiff(
            field_name="raw_ddl",
            local_value=local_ddl[:200] if local_ddl else None,
            remote_value=remote_ddl[:200] if remote_ddl else None,
            message="DDL definition differs (raw comparison)",
        )]
    return []


def compare_schemas(local: Schema, live: Schema) -> list[SchemaDiff]:
    """Compare two Schema objects and return a list of differences.

    Args:
        local: Schema from local snapshot files.
        live: Schema from the live database.

    Returns:
        List of SchemaDiff objects. Empty list means schemas are in sync.
    """
    results: list[SchemaDiff] = []

    type_map: list[tuple[str, dict, dict]] = [
        ("table", local.tables, live.tables),
        ("view", local.views, live.views),
        ("materialized_view", local.materialized_views, live.materialized_views),
        ("dictionary", local.dictionaries, live.dictionaries),
    ]

    for obj_type, local_objs, live_objs in type_map:
        all_names = dict.fromkeys(list(local_objs.keys()) + list(live_objs.keys()))

        for name in all_names:
            local_obj = local_objs.get(name)
            live_obj = live_objs.get(name)

            if local_obj and not live_obj:
                results.append(SchemaDiff(
                    name=name, obj_type=obj_type, status=DiffStatus.LOCAL_ONLY,
                ))
            elif live_obj and not local_obj:
                results.append(SchemaDiff(
                    name=name, obj_type=obj_type, status=DiffStatus.REMOTE_ONLY,
                ))
            else:
                # Both exist — compare
                field_diffs: list[FieldDiff] = []

                if obj_type == "table" and isinstance(local_obj, TableDefinition) and isinstance(live_obj, TableDefinition):
                    field_diffs = _compare_tables(local_obj, live_obj)
                else:
                    # Fallback to raw DDL comparison for views, MVs, dicts
                    local_ddl = getattr(local_obj, "raw_ddl", "")
                    live_ddl = getattr(live_obj, "raw_ddl", "")
                    field_diffs = _compare_raw_ddl(local_ddl, live_ddl)

                if field_diffs:
                    results.append(SchemaDiff(
                        name=name, obj_type=obj_type,
                        status=DiffStatus.MODIFIED, field_diffs=field_diffs,
                    ))
                else:
                    results.append(SchemaDiff(
                        name=name, obj_type=obj_type, status=DiffStatus.IN_SYNC,
                    ))

    return results
