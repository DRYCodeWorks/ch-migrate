"""Version-table engine selection and read barriers, without converting existing tables."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

from clickhouse_connect.cc_sqlalchemy.ddl.tableengine import MergeTree, ReplicatedMergeTree
from clickhouse_connect.cc_sqlalchemy.sql import format_table
from clickhouse_connect.cc_sqlalchemy.sql.ddlcompiler import ChDDLCompiler
from clickhouse_connect.driver.binding import quote_identifier


@dataclass(frozen=True)
class VersionTableState:
    database: str
    database_engine: str
    cluster: str | None = None
    table_engine: str | None = None

    @property
    def on_cluster(self) -> str | None:
        if self.database_engine == "Replicated" or self.database_engine.startswith("Shared"):
            return None
        return self.cluster

    @property
    def health_cluster(self) -> str | None:
        if self.database_engine.startswith("Shared"):
            return None
        return self.cluster or (self.database if self.database_engine == "Replicated" else None)

    def new_engine(self):
        if self.database_engine.startswith("Shared"):
            return MergeTree(order_by="version_num")
        if self.database_engine == "Replicated":
            return ReplicatedMergeTree(order_by="version_num")
        if self.cluster:
            path = (
                f"/clickhouse/ch_migrate/{quote(self.database, safe='')}/{{shard}}/alembic_version"
            )
            return ReplicatedMergeTree(order_by="version_num", zk_path=path, replica="{replica}")
        return MergeTree(order_by="version_num")

    def warning(self) -> str | None:
        replicated = self.database_engine == "Replicated" or bool(self.cluster)
        if self.database_engine.startswith("Shared") or not replicated or not self.table_engine:
            return None
        if self.table_engine.startswith(("Replicated", "Shared")):
            return None
        return (
            f"{self.database}.alembic_version uses non-replicated {self.table_engine} on a "
            "replicated deployment. Existing tables are never converted automatically. "
            "Pause migration runners, back up and reconcile the authoritative heads, then "
            "recreate replicated state and copy it once. Follow README 'The version table' "
            "before routing migrations across nodes."
        )


class VersionTableDDLCompiler(ChDDLCompiler):
    """Add ON CLUSTER only to version-table metadata marked by our implementation."""

    def visit_create_table(self, create, **kwargs):
        sql = super().visit_create_table(create, **kwargs)
        cluster = create.element.info.get("ch_migrate_on_cluster")
        if cluster:
            name = format_table(create.element)
            sql = sql.replace(name, f"{name} ON CLUSTER {quote_identifier(cluster)}", 1)
        return sql


class VersionTableMutationError(RuntimeError):
    """An unfinished version-table mutation has a server-reported failure."""


def inspect_version_table(client, database: str, cluster: str | None = None) -> VersionTableState:
    rows = client.query(
        "SELECT engine FROM system.databases WHERE name = {db:String}",
        parameters={"db": database},
    ).result_rows
    if not rows:
        raise ValueError(f"Database {database!r} does not exist; run bootstrap first")
    database_engine = rows[0][0]
    tables = client.query(
        "SELECT engine FROM system.tables WHERE database = {db:String} AND name = 'alembic_version'",
        parameters={"db": database},
    ).result_rows
    return VersionTableState(database, database_engine, cluster, tables[0][0] if tables else None)


def sync_version_replica(client, state: VersionTableState) -> VersionTableState:
    if state.database_engine == "Replicated":
        client.command(f"SYSTEM SYNC DATABASE REPLICA {quote_identifier(state.database)}")
        state = inspect_version_table(client, state.database, state.cluster)
    if state.table_engine and state.table_engine.startswith("Replicated"):
        # Fetch inserted version rows without waiting for a deliberately held DELETE mutation.
        client.command(
            f"SYSTEM SYNC REPLICA {quote_identifier(state.database)}.alembic_version LIGHTWEIGHT"
        )
    return state


def assert_version_mutations_healthy(client, state: VersionTableState) -> None:
    source = "system.mutations"
    parameters = {"db": state.database}
    settings = None
    if state.health_cluster:
        source = "clusterAllReplicas({cluster:String}, system.mutations)"
        parameters["cluster"] = state.health_cluster
        # Unavailable hosts cannot supply a failure; later completion still must wait for them.
        settings = {"skip_unavailable_shards": 1}
    failed = client.query(
        f"SELECT materialize(hostName()), mutation_id, latest_fail_reason FROM {source} "
        "WHERE database = {db:String} AND table = 'alembic_version' "
        "AND NOT is_done AND latest_fail_reason != '' ORDER BY create_time, mutation_id LIMIT 1",
        parameters=parameters,
        settings=settings,
    ).result_rows
    if failed:
        host, mutation, reason = failed[0]
        raise VersionTableMutationError(
            f"Version-table mutation {mutation} on {host} failed: {reason}. "
            "Resolve the failed mutation manually before retrying; ch-migrate did not kill it."
        )
