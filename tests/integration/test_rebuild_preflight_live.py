"""Read-only preflight against owned ClickHouse writer profiles and live mutations."""

import secrets
from dataclasses import replace

import clickhouse_connect
import pytest

from ch_migrate.introspect import get_live_schema
from ch_migrate.rebuild_preflight import RebuildRequest, inspect_rebuild

pytestmark = pytest.mark.integration


def test_preflight_accepts_synchronous_default_writer(project):
    request = _source(project)
    project.client.command(f"INSERT INTO {project.database}.events VALUES (1, 2)")
    project.client.command("SYSTEM FLUSH LOGS")
    result = inspect_rebuild(project.client, request)
    assert not [item for item in result.findings if item.severity == "refusal"]
    assert result.part_count == 1
    assert result.partition_parts == {"all": 1}
    assert result.insert_rows_per_second == pytest.approx(1 / 3600)


def test_preflight_detects_inherited_fire_and_forget_profile(project, clickhouse_server):
    request = _source(project)
    name = "writer_" + secrets.token_hex(6)
    password = secrets.token_hex(24)
    project.client.command(
        f"CREATE SETTINGS PROFILE {name} SETTINGS async_insert = 1, wait_for_async_insert = 0, log_query_settings = 0"
    )
    project.client.command(f"CREATE USER {name} IDENTIFIED BY '{password}' SETTINGS PROFILE {name}")
    try:
        project.client.command(f"GRANT INSERT ON {project.database}.events TO {name}")
        writer = clickhouse_connect.get_client(
            host=clickhouse_server.host,
            port=clickhouse_server.port,
            username=name,
            password=password,
        )
        try:
            writer.command(f"INSERT INTO {project.database}.events VALUES (1, 2)")
        finally:
            writer.close()
        project.client.command("SYSTEM FLUSH LOGS")
        settings = project.client.query(
            "SELECT Settings FROM system.query_log WHERE user = {user:String} AND query_kind = 'Insert'",
            parameters={"user": name},
        ).result_rows
        assert settings and all(not row[0] for row in settings)
        result = inspect_rebuild(project.client, request)
        risky = [item for item in result.findings if item.code == "unacknowledged_async_writer"]
        assert len(risky) == 1
        assert risky[0].severity == "refusal"
        assert risky[0].details["user"] == name
        allowed = inspect_rebuild(
            project.client, replace(request, allow_unacknowledged_async_loss=True)
        )
        assert all(
            item.severity == "warning"
            for item in allowed.findings
            if item.code == "unacknowledged_async_writer"
        )
    finally:
        project.client.command(f"DROP USER IF EXISTS {name}")
        project.client.command(f"DROP SETTINGS PROFILE IF EXISTS {name}")


def test_preflight_checks_both_replicas_and_distributed_routes(cluster_project, clickhouse_cluster):
    project, cluster = cluster_project, clickhouse_cluster
    table = f"{project.database}.events"
    project.client.command(
        f"CREATE TABLE {table} ON CLUSTER {cluster.name} (id UInt64, value UInt64) "
        "ENGINE = ReplicatedMergeTree('/chm-preflight/{database}/{table}', '{replica}') ORDER BY id"
    )
    cluster.clients[2].command(f"INSERT INTO {table} VALUES (1, 2)")
    project.client.command(f"SYSTEM SYNC REPLICA {table}")
    for client in cluster.clients.values():
        client.command("SYSTEM FLUSH LOGS")
    source = get_live_schema(project.client, project.database).tables["events"]
    request = RebuildRequest(project.database, source, replace(source, name="new"), cluster.name)
    result = inspect_rebuild(project.client, request)
    assert not [item for item in result.findings if item.severity == "refusal"]
    assert result.part_count == 1  # Replica copies are not two logical parts.
    assert result.insert_rows_per_second == pytest.approx(1 / 3600)
    project.client.command(
        f"CREATE TABLE {project.database}.route ON CLUSTER {cluster.name} AS {table} "
        f"ENGINE = Distributed('{cluster.name}', '{project.database}', 'events')"
    )
    routed = inspect_rebuild(project.client, request)
    routes = [item for item in routed.findings if item.code == "distributed_route"]
    assert len({item.details["host"] for item in routes}) == 2
    assert all(item.severity == "refusal" for item in routes)


def test_preflight_refuses_pending_mutation_and_partition_change(project):
    request = _source(project)
    table = f"{project.database}.events"
    project.client.command(f"INSERT INTO {table} VALUES (1, 2)")
    project.client.command(f"SYSTEM STOP MERGES {table}")
    try:
        project.client.command(
            f"ALTER TABLE {table} UPDATE value = value + 1 WHERE 1 SETTINGS mutations_sync = 0"
        )
        target = replace(request.target, partition_by="id")
        result = inspect_rebuild(project.client, replace(request, target=target))
        codes = {item.code for item in result.findings if item.severity == "refusal"}
        assert {"unfinished_mutations", "partition_key_change"} <= codes
    finally:
        project.client.command(f"SYSTEM START MERGES {table}")


def _source(project):
    project.client.command(
        f"CREATE TABLE {project.database}.events (id UInt64, value UInt64) ENGINE = MergeTree ORDER BY id"
    )
    source = get_live_schema(project.client, project.database).tables["events"]
    return RebuildRequest(
        project.database, source, replace(source, name="replacement", order_by=["value"])
    )
