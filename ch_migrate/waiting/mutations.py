"""Wait until a database has no unfinished mutations on any replica that has to apply them.

The waiter doesn't track which statement started which mutation. After each write it
records the unfinished mutations the connected server reports, then polls until each one
is done everywhere it applies: on the service for SharedMergeTree, on every replica for
ReplicatedMergeTree (a replica that hasn't pulled the mutation yet is still pending), and
where it runs for anything else. Every unfinished mutation in the database counts, so a
migration also waits for, and is stopped by, another writer's work.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from clickhouse_connect.driver.exceptions import DatabaseError

from ch_migrate.waiting import reruns
from ch_migrate.waiting.display import HostParts, MutationProgress
from ch_migrate.waiting.errors import FailedMutation, MutationFailed, WaitingError
from ch_migrate.waiting.sql import sql_string

SHARED = "shared"
# ch-migrate's own tables: version_table.py waits on the version table's DELETE itself and
# refuses a stuck one before the run starts, and the rerun markers' DELETE waits the same way.
INTERNAL_TABLES = ("alembic_version", reruns.TABLE)


class MutationWaiter:
    """Polls `system.mutations` on the migration session; never kills or resends anything."""

    def __init__(self, client, state, budget):
        self.client, self.state, self.budget = client, state, budget
        self._hosts: dict[str, tuple[str, ...] | None] = {}
        self._part_sizes: dict[tuple[str, str], int] = {}

    def wait(self, before_run: bool = False) -> None:
        """Return once every unfinished mutation seen now has finished where it applies."""
        self._hosts.clear()
        expected = {(row.table, row.mutation) for row in self._rows(set())}
        while expected:
            rows = self._rows(expected)
            expected |= {(row.table, row.mutation) for row in rows if not row.done}
            _raise_failures(self.state.database, rows, before_run)
            pending = self._pending(expected, rows)
            if not pending:
                self.budget.complete(f"{self.state.database} mutations")
                return
            self.budget.pause(_pending_text(self.state.database, pending), self._snapshot(pending))

    def _rows(self, expected: set[tuple[str, str]]) -> list[_Row]:
        """Unfinished mutations in the database, and every row for the ones already expected."""
        cluster = self.state.health_cluster
        source = (
            "clusterAllReplicas({cluster:String}, system.mutations)"
            if cluster
            else "system.mutations"
        )
        rows = self.client.query(
            "SELECT materialize(hostName()), table, mutation_id, is_done, parts_to_do, "
            f"latest_fail_reason, parts_to_do_names FROM {source} WHERE database = {{db:String}} "
            "AND NOT has({internal:Array(String)}, table) "
            "AND (NOT is_done OR has({expected:Array(String)}, concat(table, '/', mutation_id)))",
            parameters={
                "db": self.state.database,
                "internal": list(INTERNAL_TABLES),
                "cluster": cluster,
                "expected": sorted(f"{table}/{mutation}" for table, mutation in expected),
            },
            settings=_cluster_settings() if cluster else None,
        ).result_rows
        return [self._row(values) for values in rows]

    def _row(self, values: tuple) -> _Row:
        host, table, mutation, done, parts, reason, names = values
        # Every replica of a SharedMergeTree table reports the same shared mutation.
        if self._required_hosts(table) == (SHARED,):
            host = SHARED
        return _Row(host, table, mutation, bool(done), parts, reason, frozenset(names))

    def _pending(self, expected: set[tuple[str, str]], rows: list[_Row]) -> list[_Row]:
        """Unfinished rows, plus a placeholder for each replica that hasn't reported one yet."""
        pending = [row for row in rows if not row.done]
        seen = {(row.host, row.table, row.mutation) for row in rows}
        for table, mutation in sorted(expected):
            for host in self._required_hosts(table) or ():
                if host != SHARED and (host, table, mutation) not in seen:
                    pending.append(
                        _Row(host, table, mutation, False, 0, "", frozenset(), missing=True)
                    )
        return pending

    def _required_hosts(self, table: str) -> tuple[str, ...] | None:
        """Hosts that must each report a mutation done; None means wherever it appears."""
        if table not in self._hosts:
            self._hosts[table] = self._resolve_hosts(table)
        return self._hosts[table]

    def _resolve_hosts(self, table: str) -> tuple[str, ...] | None:
        rows = self.client.query(
            "SELECT engine FROM system.tables WHERE database = {db:String} AND name = {table:String}",
            parameters={"db": self.state.database, "table": table},
        ).result_rows
        engine = rows[0][0] if rows else ""
        if engine.startswith("Shared"):
            return (SHARED,)
        if engine.startswith("Replicated"):
            return self._replicas(table)
        return None

    def _replicas(self, table: str) -> tuple[str, ...]:
        path, total = self.client.query(
            "SELECT zookeeper_path, total_replicas FROM system.replicas "
            "WHERE database = {db:String} AND table = {table:String}",
            parameters={"db": self.state.database, "table": table},
        ).result_rows[0]
        cluster = self.state.health_cluster
        if not cluster:
            if total > 1:
                raise WaitingError(
                    f"{self.state.database}.{table} has {total} replicas; set `cluster` in "
                    "config.yaml so `up` can wait for the mutation on every one"
                )
            return (self.client.command("SELECT hostName()"),)
        while len(hosts := self._cluster_replicas(table, path)) < total:
            self.budget.pause(
                f"{self.state.database}.{table}: cluster {cluster} shows {len(hosts)}/{total} replicas"
            )
        return hosts

    def _cluster_replicas(self, table: str, path: str) -> tuple[str, ...]:
        """Hosts the cluster can reach that hold a replica of this table's Keeper path."""
        rows = self.client.query(
            "SELECT DISTINCT materialize(hostName()) FROM clusterAllReplicas({cluster:String}, "
            "system.replicas) WHERE database = {db:String} AND table = {table:String} "
            "AND zookeeper_path = {path:String}",
            parameters={
                "cluster": self.state.health_cluster,
                "db": self.state.database,
                "table": table,
                "path": path,
            },
            settings=_cluster_settings(),
        ).result_rows
        return tuple(sorted(host for (host,) in rows))

    def _snapshot(self, pending: list[_Row]) -> MutationProgress:
        """The oldest pending mutation, as the display shows it."""
        table, mutation = min(((row.table, row.mutation) for row in pending), key=_mutation_order)
        rows = [row for row in pending if (row.table, row.mutation) == (table, mutation)]
        # A replica that hasn't received the mutation has every part its peers report.
        most = max((len(row.names) or row.parts for row in rows if not row.missing), default=0)
        hosts = [
            HostParts(row.host, row.names, most if row.missing else len(row.names) or row.parts)
            for row in rows
        ]
        if self.budget.display.wants_bytes:
            hosts = self._with_sizes(table, hosts)
        return MutationProgress(f"{self.state.database}.{table}", mutation, tuple(hosts))

    def _with_sizes(self, table: str, hosts: list[HostParts]) -> list[HostParts]:
        # Parts never change size, so each is read once; only new names cost a query.
        wanted = {(host.host, name) for host in hosts for name in host.names}
        unknown = {name for _, name in wanted - self._part_sizes.keys()}
        if unknown:
            try:
                self._part_sizes.update(self._read_sizes(table, unknown))
            except DatabaseError as error:
                self.budget.display.parts_only(_sizes_unreadable(error))
                return hosts
        sizes = self._part_sizes
        return [
            replace(
                host,
                sizes={n: sizes[(host.host, n)] for n in host.names if (host.host, n) in sizes},
            )
            for host in hosts
        ]

    def _read_sizes(self, table: str, names: set[str]) -> dict[tuple[str, str], int]:
        cluster = self.state.health_cluster
        source = "clusterAllReplicas({cluster:String}, system.parts)" if cluster else "system.parts"
        rows = self.client.query(
            f"SELECT materialize(hostName()), name, bytes_on_disk FROM {source} "
            "WHERE database = {db:String} AND table = {table:String} AND active "
            "AND has({names:Array(String)}, name)",
            parameters={
                "cluster": cluster,
                "db": self.state.database,
                "table": table,
                "names": sorted(names),
            },
            settings=_cluster_settings() if cluster else None,
        ).result_rows
        shared = self._required_hosts(table) == (SHARED,)
        return {(SHARED if shared else host, name): size for host, name, size in rows}


@dataclass(frozen=True)
class _Row:
    """One host's view of one mutation, or a placeholder for a replica that hasn't got it."""

    host: str
    table: str
    mutation: str
    done: bool
    parts: int
    reason: str
    names: frozenset[str]
    missing: bool = False


def _raise_failures(database: str, rows: list[_Row], before_run: bool) -> None:
    for row in rows:
        if row.done or not row.reason:
            continue
        kill = (
            f"KILL MUTATION WHERE database = {sql_string(database)} "
            f"AND table = {sql_string(row.table)} AND mutation_id = {sql_string(row.mutation)}"
        )
        table = f"{database}.{row.table}"
        prefix = "Blocked by a failed mutation" if before_run else "Mutation failed"
        raise MutationFailed(
            f"{prefix}: {table} mutation {row.mutation} on {row.host}: {row.reason}\n"
            f"Operator only on {row.host}: {kill}",
            FailedMutation(table, row.mutation, row.host, row.reason, kill, before_run),
        )


def _pending_text(database: str, pending: list[_Row]) -> str:
    parts = []
    for row in sorted(pending, key=lambda row: (row.table, _order(row.mutation), row.host)):
        where = f"{database}.{row.table} mutation {row.mutation} host {row.host}"
        parts.append(
            f"{where}; not received yet" if row.missing else f"{where}; parts_to_do={row.parts}"
        )
    return "; ".join(parts)


def _mutation_order(key: tuple[str, str]) -> tuple[int, str]:
    return _order(key[1]), key[0]


def _order(mutation: str) -> int:
    numbers = re.findall(r"\d+", mutation)
    return int(numbers[-1]) if numbers else 0


def _cluster_settings() -> dict:
    # A replica that's down is skipped, so its missing rows read as pending, not as done.
    return {
        "skip_unavailable_shards": 1,
        "connect_timeout_with_failover_ms": 250,
        "receive_timeout": 2,
    }


def _sizes_unreadable(error: Exception) -> str:
    code = re.search(r"\(([A-Z][A-Z_]+)\)", str(error))
    why = f" ({code.group(1)})" if code else ""
    return (
        f"can't read system.parts{why}. Grant SELECT ON system.parts to the migration role "
        "to see bytes and a time estimate."
    )
