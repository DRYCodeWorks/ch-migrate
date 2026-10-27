"""ON CLUSTER statements finish on every host before a migration is recorded."""

import pytest

pytestmark = pytest.mark.integration
NOT_FINISHED = "is not finished on 1 of 2 hosts"


def test_on_cluster_create_then_alter_finishes_every_host(cluster_project, clickhouse_cluster):
    project, cluster = cluster_project, clickhouse_cluster
    _baseline(project)
    _revision(
        project,
        [
            f"CREATE TABLE IF NOT EXISTS {{db}}.owned ON CLUSTER {cluster.name} (id UInt64) ENGINE=Memory",
            f"ALTER TABLE {{db}}.owned ON CLUSTER {cluster.name} ADD COLUMN IF NOT EXISTS value String",
        ],
    )
    result = project.run("up", "it")
    assert result.exit_code == 0, result.output
    for client in cluster.clients.values():
        assert client.query(
            "SELECT name FROM system.columns WHERE database={db:String} AND table='owned' ORDER BY position",
            parameters={"db": project.database},
        ).result_rows == [("id",), ("value",)]
    assert _heads(project) == ["bbbb"]


def test_on_cluster_down_host_times_out_then_runs_again(cluster_project, clickhouse_cluster):
    project, cluster = cluster_project, clickhouse_cluster
    _baseline(project)
    _revision(
        project,
        [
            f"CREATE TABLE IF NOT EXISTS {{db}}.owned ON CLUSTER {cluster.name} (id UInt64) ENGINE=Memory"
        ],
    )
    cluster.stop_node(2)
    try:
        result = project.run("up", "it", "--timeout", "2")
        assert result.exit_code == 1 and NOT_FINISHED in result.output, result.output
        assert _heads(project) == ["aaaa"]
    finally:
        cluster.start_node(2)
    resumed = project.run("up", "it")
    assert resumed.exit_code == 0, resumed.output
    for client in cluster.clients.values():
        assert client.command(f"EXISTS TABLE {project.database}.owned") == 1
    assert _heads(project) == ["bbbb"]


def test_on_cluster_host_exception_fails_the_run(cluster_project, clickhouse_cluster):
    project, cluster = cluster_project, clickhouse_cluster
    _baseline(project)
    table = f"{project.database}.owned"
    project.client.command(
        f"CREATE TABLE {table} ON CLUSTER {cluster.name} (id UInt64) ENGINE=Memory"
    )
    cluster.clients[2].command(f"ALTER TABLE {table} ADD COLUMN conflict String")
    _revision(
        project, [f"ALTER TABLE {{db}}.owned ON CLUSTER {cluster.name} ADD COLUMN conflict UInt64"]
    )
    result = project.run("up", "it")
    assert result.exit_code == 1, result.output
    assert cluster.hostnames[2] in result.output and "DUPLICATE_COLUMN" in result.output
    assert _heads(project) == ["aaaa"]


def test_on_cluster_and_replicated_mutation_both_must_finish(cluster_project, clickhouse_cluster):
    project, cluster = cluster_project, clickhouse_cluster
    _baseline(project)
    table = f"{project.database}.owned"
    project.client.command(
        f"CREATE TABLE {table} ON CLUSTER {cluster.name} (id UInt64, x UInt64) "
        f"ENGINE=ReplicatedMergeTree('/clickhouse/ddl_wait/{project.database}', '{{replica}}') ORDER BY id"
    )
    project.client.command(f"INSERT INTO {table} VALUES (1, 0)")
    cluster.clients[2].command(f"SYSTEM SYNC REPLICA {table}")
    _revision(
        project, [f"ALTER TABLE {{db}}.owned ON CLUSTER {cluster.name} UPDATE x=1 WHERE id=1"]
    )
    for client in cluster.clients.values():
        client.command(f"SYSTEM STOP MERGES {table}")
    try:
        # Long enough for the queue entry to finish on both hosts on a slow CI runner, so the
        # deadline falls in the mutation wait that STOP MERGES holds open.
        result = project.run("up", "it", "--timeout", "5")
        assert result.exit_code == 1 and "parts_to_do=" in result.output, result.output
        assert _heads(project) == ["aaaa"]
    finally:
        for client in cluster.clients.values():
            client.command(f"SYSTEM START MERGES {table}")
    resumed = project.run("up", "it")
    assert resumed.exit_code == 0, resumed.output
    for client in cluster.clients.values():
        assert client.command(f"SELECT x FROM {table}") == 1
    assert _heads(project) == ["bbbb"]


def test_on_cluster_ttl_materialization_is_waited(cluster_project, clickhouse_cluster):
    """ClickHouse holds MODIFY TTL until it's materialized, so the DDL limit bounds that wait."""
    project, cluster = cluster_project, clickhouse_cluster
    _baseline(project)
    table = f"{project.database}.expiry"
    project.client.command(
        f"CREATE TABLE {table} ON CLUSTER {cluster.name} (ts DateTime) "
        f"ENGINE=ReplicatedMergeTree('/clickhouse/ddl_ttl/{project.database}', '{{replica}}') ORDER BY ts"
    )
    project.client.command(f"INSERT INTO {table} VALUES ('2020-01-01 00:00:00')")
    cluster.clients[2].command(f"SYSTEM SYNC REPLICA {table}")
    _revision(
        project,
        [f"ALTER TABLE {{db}}.expiry ON CLUSTER {cluster.name} MODIFY TTL ts + INTERVAL 100 YEAR"],
    )
    for client in cluster.clients.values():
        client.command(f"SYSTEM STOP MERGES {table}")
    try:
        result = project.run("up", "it", "--timeout", "3")
        assert result.exit_code == 1 and "is not finished on" in result.output, result.output
        assert _heads(project) == ["aaaa"]
    finally:
        for client in cluster.clients.values():
            client.command(f"SYSTEM START MERGES {table}")
    resumed = project.run("up", "it")
    assert resumed.exit_code == 0, resumed.output
    for client in cluster.clients.values():
        assert not client.command(
            "SELECT count() FROM system.mutations WHERE database = {db:String} AND NOT is_done",
            parameters={"db": project.database},
        )
    assert _heads(project) == ["bbbb"]


def test_on_cluster_nonreplicated_mutations_wait_on_each_host(cluster_project, clickhouse_cluster):
    project, cluster = cluster_project, clickhouse_cluster
    _baseline(project)
    table = f"{project.database}.owned"
    project.client.command(
        f"CREATE TABLE {table} ON CLUSTER {cluster.name} (x UInt64) ENGINE=MergeTree ORDER BY tuple()"
    )
    for client in cluster.clients.values():
        client.command(f"INSERT INTO {table} VALUES (0)")
    _revision(project, [f"ALTER TABLE {{db}}.owned ON CLUSTER {cluster.name} UPDATE x=1 WHERE 1"])
    cluster.clients[2].command(f"SYSTEM STOP MERGES {table}")
    try:
        result = project.run("up", "it", "--timeout", "2")
        assert result.exit_code == 1, result.output
        assert cluster.hostnames[2] in result.output
        assert _heads(project) == ["aaaa"]
        assert cluster.clients[1].command(f"SELECT x FROM {table}") == 1
        assert cluster.clients[2].command(f"SELECT x FROM {table}") == 0
    finally:
        cluster.clients[2].command(f"SYSTEM START MERGES {table}")
    resumed = project.run("up", "it")
    assert resumed.exit_code == 0, resumed.output
    for client in cluster.clients.values():
        assert client.command(f"SELECT x FROM {table}") == 1


def test_on_cluster_cold_version_table_creation_obeys_timeout(cluster_project, clickhouse_cluster):
    project, cluster = cluster_project, clickhouse_cluster
    project.write_revision("aaaa", {"upgrade": 'op.execute("SELECT 1")'})
    cluster.stop_node(2)
    try:
        result = project.run("up", "it", "--timeout", "1")
        assert result.exit_code == 1 and NOT_FINISHED in result.output, result.output
        assert _heads(project) == []
    finally:
        cluster.start_node(2)
    resumed = project.run("up", "it")
    assert resumed.exit_code == 0, resumed.output
    assert _heads(project) == ["aaaa"]


def _baseline(project):
    project.write_revision("aaaa", {"upgrade": 'op.execute("SELECT 1")'})
    result = project.run("up", "it")
    assert result.exit_code == 0, result.output


def _revision(project, statements):
    path = project.sql_dir / "ddl.sql"
    path.write_text(
        "\n".join(
            "-- ch-migrate: allow-non-idempotent Controlled distributed DDL scenario\n" + sql + ";"
            for sql in statements
        )
    )
    project.write_revision(
        "bbbb", {"upgrade": "from ch_migrate import run_sql\nrun_sql('ddl.sql')"}, "aaaa"
    )


def _heads(project):
    return [
        row[0]
        for row in project.client.query(
            f"SELECT version_num FROM {project.database}.alembic_version ORDER BY version_num"
        ).result_rows
    ]
