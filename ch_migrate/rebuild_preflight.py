"""Read-only, fail-closed assessment of a proposed table rebuild."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from ch_migrate.classify import _tokens
from ch_migrate.introspect import TableDefinition

_WINDOW_MINUTES = 60
_LOG_LIMIT = 10001
_SETTING_NAMES = ("async_insert", "wait_for_async_insert")
_REPLACING = {"ReplacingMergeTree", "ReplicatedReplacingMergeTree", "SharedReplacingMergeTree"}


@dataclass(frozen=True)
class RebuildRequest:
    database: str
    source: TableDefinition
    target: TableDefinition
    cluster: str | None = None
    allow_unacknowledged_async_loss: bool = False
    transfer_pairs: tuple[tuple[TableDefinition, TableDefinition], ...] = ()


@dataclass(frozen=True)
class PreflightFinding:
    code: str
    severity: Literal["refusal", "warning"]
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RebuildAssessment:
    findings: tuple[PreflightFinding, ...]
    bytes_on_disk: int
    part_count: int
    partition_parts: dict[str, int]
    insert_rows_per_second: float
    engine_note: str


def inspect_rebuild(client: Any, request: RebuildRequest) -> RebuildAssessment:
    """Inspect live state; inspection errors propagate instead of implying safety."""
    scope = _Scope(client, request.cluster)
    findings = _definition_findings(request)
    findings.extend(_topology_findings(scope, request))
    findings.extend(_mutation_findings(scope, request))
    storage = _parts(scope, request)
    findings.extend(_capacity_findings(scope, request, storage))
    rate, writers = _writer_log(scope, request)
    findings.extend(_writer_findings(scope, request, writers))
    findings.extend(_dedup_findings(scope, request))
    return RebuildAssessment(
        tuple(findings),
        storage.bytes_on_disk,
        storage.part_count,
        storage.partition_parts,
        rate,
        _engine_note(request),
    )


@dataclass(frozen=True)
class _Storage:
    bytes_on_disk: int
    part_count: int
    partition_parts: dict[str, int]
    host_partitions: dict[tuple[str, str], int]
    host_disks: dict[tuple[str, str], int]


@dataclass(frozen=True)
class _Scope:
    client: Any
    cluster: str | None

    def table(self, table: str) -> str:
        if self.cluster:
            return f"clusterAllReplicas({_literal(self.cluster)}, system.{table})"
        return f"system.{table}"

    def log(self) -> str:
        merged = "merge('system', '^query_log(_[0-9]+)?$')"
        if self.cluster:
            return f"clusterAllReplicas({_literal(self.cluster)}, {merged})"
        return merged

    def rows(self, sql: str) -> list[tuple[Any, ...]]:
        return self.client.query(sql).result_rows


def _definition_findings(request: RebuildRequest) -> list[PreflightFinding]:
    findings = []
    if request.source.engine.split("(", 1)[0] == "Distributed":
        findings.append(
            PreflightFinding(
                "distributed_source",
                "refusal",
                "A Distributed source cannot be rebuilt atomically.",
            )
        )
    if _normalized(request.source.partition_by or "tuple()") != _normalized(
        request.target.partition_by or "tuple()"
    ):
        findings.append(
            PreflightFinding(
                "partition_key_change", "refusal", "Rebuild 1.0 cannot change the partition key."
            )
        )
    for source, destination in request.transfer_pairs:
        differences = _transfer_differences(source, destination)
        if differences:
            findings.append(
                PreflightFinding(
                    "transfer_structure_mismatch",
                    "refusal",
                    "Physical partition transfer requires matching structures and engines.",
                    {
                        "source": source.name,
                        "destination": destination.name,
                        "differences": differences,
                    },
                )
            )
    return findings


def _transfer_differences(source: TableDefinition, destination: TableDefinition) -> list[str]:
    differences = []
    if _normalized(source.engine) != _normalized(destination.engine):
        differences.append("engine")
    if _normalized(" ".join(source.order_by)) != _normalized(" ".join(destination.order_by)):
        differences.append("sorting_key")
    if _normalized(source.partition_by or "tuple()") != _normalized(
        destination.partition_by or "tuple()"
    ):
        differences.append("partition_key")
    physical = lambda table: [
        (col.name, _normalized(col.type)) for col in table.columns if col.default_kind != "ALIAS"
    ]
    if physical(source) != physical(destination):
        differences.append("physical_columns")
    return differences


def _topology_findings(scope: _Scope, request: RebuildRequest) -> list[PreflightFinding]:
    findings = []
    if request.cluster:
        shards = scope.rows(
            f"SELECT uniqExact(shard_num) FROM system.clusters WHERE cluster = {_literal(request.cluster)}"
        )
        if not shards or not shards[0][0]:
            raise RuntimeError(f"Configured ClickHouse cluster {request.cluster!r} is not visible")
        if shards[0][0] > 1:
            findings.append(
                PreflightFinding(
                    "sharded_cluster",
                    "refusal",
                    "Rebuild cannot swap atomically across shards.",
                    {"shards": int(shards[0][0])},
                )
            )
    sql = (
        "SELECT materialize(hostName()), database, name, engine_full FROM "
        + scope.table("tables")
        + " WHERE engine = 'Distributed'"
    )
    for host, database, name, engine in scope.rows(sql):
        route = re.match(
            r"^Distributed\s*\(\s*(['`\"]?)[^,]+?\1\s*,\s*(['`\"]?)([^,'`\"\s)]+)\2\s*,\s*(['`\"]?)([^,'`\"\s)]+)\4",
            engine,
            re.I,
        )
        if route and route.group(3) == request.database and route.group(5) == request.source.name:
            findings.append(
                PreflightFinding(
                    "distributed_route",
                    "refusal",
                    "A Distributed table routes inserts to this source.",
                    {"host": str(host), "table": f"{database}.{name}"},
                )
            )
        elif request.source.name in engine and not route:
            findings.append(
                PreflightFinding(
                    "distributed_route_unknown",
                    "refusal",
                    "Cannot exclude a Distributed route to this source.",
                    {"host": str(host), "table": f"{database}.{name}"},
                )
            )
    return findings


def _mutation_findings(scope: _Scope, request: RebuildRequest) -> list[PreflightFinding]:
    sql = (
        "SELECT materialize(hostName()), mutation_id FROM "
        + scope.table("mutations")
        + f" WHERE database = {_literal(request.database)} AND table = {_literal(request.source.name)} AND is_done = 0"
    )
    rows = scope.rows(sql)
    if not rows:
        return []
    return [
        PreflightFinding(
            "unfinished_mutations",
            "refusal",
            "Source has unfinished mutations on at least one replica.",
            {"mutations": [{"host": str(host), "id": str(mid)} for host, mid in rows]},
        )
    ]


def _parts(scope: _Scope, request: RebuildRequest) -> _Storage:
    sql = (
        "SELECT materialize(hostName()) AS host, partition_id, disk_name, count(), sum(bytes_on_disk) FROM "
        + scope.table("parts")
        + f" WHERE database = {_literal(request.database)} AND table = {_literal(request.source.name)}"
        + " AND active = 1 GROUP BY host, partition_id, disk_name"
    )
    partitions: dict[tuple[str, str], int] = {}
    disks: dict[tuple[str, str], int] = {}
    sizes: dict[str, int] = {}
    counts: dict[str, int] = {}
    for host, partition, disk, count, size in scope.rows(sql):
        partitions[host, partition] = partitions.get((host, partition), 0) + int(count)
        disks[host, disk] = disks.get((host, disk), 0) + int(size)
        sizes[host] = sizes.get(host, 0) + int(size)
        counts[host] = counts.get(host, 0) + int(count)
    maxima: dict[str, int] = {}
    for (_, partition), count in partitions.items():
        maxima[partition] = max(maxima.get(partition, 0), count)
    return _Storage(
        max(sizes.values(), default=0), max(counts.values(), default=0), maxima, partitions, disks
    )


def _capacity_findings(
    scope: _Scope, request: RebuildRequest, storage: _Storage
) -> list[PreflightFinding]:
    findings = []
    rows = scope.rows(
        "SELECT materialize(hostName()), name, unreserved_space FROM " + scope.table("disks")
    )
    disks = {(host, disk): int(free) for host, disk, free in rows}
    for (host, disk), size in storage.host_disks.items():
        if (host, disk) not in disks:
            raise RuntimeError(f"No capacity data for source disk {host}/{disk}")
        if disks[host, disk] < 3 * size:
            findings.append(
                PreflightFinding(
                    "low_disk_space",
                    "warning",
                    "Less than approximately three times source bytes are free on a source disk.",
                    {
                        "host": host,
                        "disk": disk,
                        "available_bytes": disks[host, disk],
                        "source_bytes": size,
                    },
                )
            )
    configured = request.source.settings.get("parts_to_throw_insert")
    thresholds = dict(
        scope.rows(
            "SELECT materialize(hostName()), value FROM "
            + scope.table("merge_tree_settings")
            + " WHERE name = 'parts_to_throw_insert'"
        )
    )
    for (host, partition), count in storage.host_partitions.items():
        if configured is None and host not in thresholds:
            raise RuntimeError(f"Cannot determine parts_to_throw_insert on {host}")
        threshold = int(configured if configured is not None else thresholds[host])
        if threshold > 0 and count >= threshold * 0.8:
            findings.append(
                PreflightFinding(
                    "many_parts",
                    "warning",
                    "Partition approaches parts_to_throw_insert.",
                    {
                        "host": host,
                        "partition_id": partition,
                        "parts": count,
                        "threshold": threshold,
                    },
                )
            )
    return findings


def _writer_log(scope: _Scope, request: RebuildRequest) -> tuple[float, list[dict[str, Any]]]:
    sql = (
        "SELECT materialize(hostName()), query_id, type, user, current_database, tables, query, Settings, written_rows, query_start_time "
        + "FROM "
        + scope.log()
        + " WHERE query_start_time >= now() - INTERVAL 60 MINUTE"
        + " AND query_kind = 'Insert' AND is_initial_query = 1 AND type IN ('QueryStart', 'QueryFinish')"
        + f" ORDER BY query_start_time DESC LIMIT {_LOG_LIMIT}"
    )
    rows = scope.rows(sql)
    if len(rows) >= _LOG_LIMIT:
        raise RuntimeError("Writer log exceeds bounded 60-minute inspection window")
    writers: dict[tuple[str, str], dict[str, Any]] = {}
    target = f"{request.database}.{request.source.name}"
    for host, qid, event, user, db, tables, query, settings, written, _ in rows:
        if not _inserts_target((str(query), str(db), list(tables)), target):
            continue
        key = (str(host), str(qid))
        if key not in writers or event == "QueryFinish":
            writers[key] = {
                "host": str(host),
                "user": str(user),
                "settings": dict(settings),
                "rows": int(written),
                "finished": event == "QueryFinish",
            }
    return sum(row["rows"] for row in writers.values() if row["finished"]) / (
        _WINDOW_MINUTES * 60
    ), list(writers.values())


def _inserts_target(insert: tuple[str, str, list[str]], target: str) -> bool:
    query, database, tables = insert
    db, table = target.split(".", 1)
    # QueryStart may have empty tables; the SQL prefix identifies direct writes, not MV side effects.
    prefix = re.match(
        r"^\s*INSERT\s+INTO\s+(?:TABLE\s+)?((?:[`\"]?\w+[`\"]?\.)?[`\"]?\w+[`\"]?)", query, re.I
    )
    if prefix:
        named = prefix.group(1).replace("`", "").replace('"', "")
        return named == target or (named == table and database == db)
    return target in tables and bool(re.match(r"^\s*INSERT\b", query, re.I))


def _writer_findings(
    scope: _Scope, request: RebuildRequest, writers: list[dict[str, Any]]
) -> list[PreflightFinding]:
    findings = [
        PreflightFinding(
            "insert_rate",
            "warning",
            "Recent completed INSERT throughput is reported; async fire-and-forget writes may have zero written_rows.",
            {"window_minutes": _WINDOW_MINUTES, "writer_queries": len(writers)},
        )
    ]
    if not writers:
        findings.append(
            PreflightFinding(
                "writer_history_limited",
                "warning",
                "No writers found in the bounded query-log window; this does not establish that none exist.",
            )
        )
        return findings
    profiles = _profile_data(scope)
    for writer in writers:
        effective = _effective_settings(writer, profiles)
        if effective is None:
            findings.append(
                PreflightFinding(
                    "writer_settings_unknown",
                    "refusal",
                    "Cannot prove this writer's effective async/wait settings; unlogged defaults or conflicting roles may apply.",
                    {"host": writer["host"], "user": writer["user"]},
                )
            )
        elif effective == ("1", "0"):
            severity = "warning" if request.allow_unacknowledged_async_loss else "refusal"
            findings.append(
                PreflightFinding(
                    "unacknowledged_async_writer",
                    severity,
                    "Writer uses async_insert=1 with wait_for_async_insert=0; queued unacknowledged writes can be lost at swap.",
                    {
                        "host": writer["host"],
                        "user": writer["user"],
                        "acknowledged_loss": request.allow_unacknowledged_async_loss,
                    },
                )
            )
    return findings


def _profile_data(scope: _Scope) -> dict[str, dict[str, Any]]:
    tables = {
        "profiles": "SELECT materialize(hostName()), name, apply_to_all, apply_to_list, apply_to_except FROM "
        + scope.table("settings_profiles"),
        "elements": "SELECT materialize(hostName()), profile_name, user_name, role_name, setting_name, value, inherit_profile FROM "
        + scope.table("settings_profile_elements"),
        "users": "SELECT materialize(hostName()), name, default_roles_all, default_roles_list, default_roles_except FROM "
        + scope.table("users"),
        "grants": "SELECT materialize(hostName()), user_name, role_name, granted_role_name FROM "
        + scope.table("role_grants"),
        "defaults": "SELECT materialize(hostName()), name, default FROM "
        + scope.table("settings")
        + " WHERE name IN ('async_insert', 'wait_for_async_insert')",
    }
    result: dict[str, dict[str, Any]] = {}
    for kind, sql in tables.items():
        for host, *record in scope.rows(sql):
            result.setdefault(str(host), {name: [] for name in tables})[kind].append(tuple(record))
    return result


def _effective_settings(
    writer: dict[str, Any], by_host: dict[str, dict[str, Any]]
) -> tuple[str, str] | None:
    host = by_host.get(writer["host"])
    if host is None:
        return None
    user = writer["user"]
    identity = next((row for row in host["users"] if row[0] == user), None)
    if identity is None:
        return None
    # Active roles are not recorded in query_log. Include every granted role and
    # refuse conflicting unlogged values rather than assuming default roles.
    roles = _possible_roles(user, host["grants"])
    applicable = _applicable_profiles(user, roles, host)
    values = {key: set() for key in _SETTING_NAMES}
    for profile in applicable:
        _collect_profile(profile, (host["elements"], values), set())
    _collect_assigned(user, roles, (host["elements"], values))
    defaults = dict(host["defaults"])
    for name in _SETTING_NAMES:
        if not values[name] and name in defaults:
            values[name].add(str(defaults[name]))
    explicit = writer["settings"]
    result = []
    for name in _SETTING_NAMES:
        if name in explicit:
            result.append(str(explicit[name]))
        elif len(values[name]) == 1:
            result.append(next(iter(values[name])))
        else:
            return None
    return result[0], result[1]


def _possible_roles(user: str, grants: list[tuple[Any, ...]]) -> set[str]:
    roles = {str(granted) for grantee, role, granted in grants if grantee == user}
    pending = list(roles)
    while pending:
        role = pending.pop()
        for grantee, parent, granted in grants:
            if grantee is None and parent == role and granted not in roles:
                roles.add(str(granted))
                pending.append(str(granted))
    return roles


def _applicable_profiles(user: str, roles: set[str], host: dict[str, Any]) -> set[str]:
    principals = roles | {user}
    matched = set()
    for name, all_users, included, excluded in host["profiles"]:
        if (all_users and not principals.issubset(set(excluded))) or principals.intersection(
            included
        ):
            matched.add(str(name))
    return matched


def _collect_assigned(user: str, roles: set[str], state: tuple) -> None:
    elements, values = state
    for name, assigned_user, assigned_role, setting, value, parent in elements:
        if assigned_user != user and assigned_role not in roles:
            continue
        if parent:
            _collect_profile(str(parent), (elements, values), set())
        if setting in values and value is not None:
            values[setting].add(str(value))


def _collect_profile(profile: str, state: tuple, visited: set[str]) -> None:
    elements, values = state
    if profile in visited:
        raise RuntimeError(f"Settings profile inheritance cycle at {profile!r}")
    visited.add(profile)
    for name, user, role, setting, value, parent in elements:
        if name != profile or user is not None or role is not None:
            continue
        if parent:
            _collect_profile(str(parent), (elements, values), visited)
        if setting in values and value is not None:
            values[setting].add(str(value))
    visited.remove(profile)


def _dedup_findings(scope: _Scope, request: RebuildRequest) -> list[PreflightFinding]:
    if "Replicated" not in request.source.engine and "Shared" not in request.source.engine:
        return []
    rows = scope.rows(
        "SELECT value FROM system.settings WHERE name = 'deduplicate_blocks_in_dependent_materialized_views'"
    )
    if not rows:
        raise RuntimeError("Cannot inspect dependent materialized-view deduplication")
    if str(rows[0][0]) == "0":
        return [
            PreflightFinding(
                "dependent_mv_dedup",
                "warning",
                "Dependent materialized-view block deduplication is disabled in this session; inspect writer profiles as well.",
            )
        ]
    return []


def _engine_note(request: RebuildRequest) -> str:
    engine = request.target.engine.split("(", 1)[0]
    if engine in _REPLACING:
        return "ReplacingMergeTree-family merges can collapse duplicate copies sharing the sorting key; visibility before merges is not deduplicated."
    return "Row copy may keep a small number of duplicates. Collapsing and VersionedCollapsing engines do not guarantee elimination of identical positive rows."


def _literal(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _normalized(value: str) -> str:
    return "".join(_tokens(value))
