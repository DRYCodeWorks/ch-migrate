"""Version writes fail closed and state follows the database's deployment."""

import os
import re
import secrets
import signal
import subprocess
import sys
import time

import pytest
import yaml

pytestmark = pytest.mark.integration


def test_version_table_interrupted_delete_does_not_replay(project):
    for revision, parent in (("aaaa", None), ("bbbb", "aaaa"), ("cccc", "bbbb")):
        _probe_revision(project, revision, parent)
    first = project.run("up", "it", "-r", "aaaa")
    assert first.exit_code == 0, first.output
    table = f"{project.database}.alembic_version"
    project.client.command(f"SYSTEM STOP MERGES {table}")
    process = _start_up(project)
    try:
        _wait_for(lambda: set(_heads(project)) == {"aaaa", "bbbb"})
        original_ids = _wait_for_version_delete(project, process)
        assert process.poll() is None, "up must still be waiting for the version DELETE"
        _stop_process(process)
        refused = _completed_up(project, ("--timeout", "0.4"))
        assert refused[0] != 0, refused
        current_ids = {
            row[0]
            for row in project.client.query(
                "SELECT mutation_id FROM system.mutations WHERE database = {db:String} "
                "AND table = 'alembic_version'",
                parameters={"db": project.database},
            ).result_rows
        }
        assert current_ids == original_ids
        assert _probe_counts(project) == [("aaaa", 1), ("bbbb", 1)]
    finally:
        _stop_process(process)
        project.client.command(f"SYSTEM START MERGES {table}")
    _wait_for(lambda: _heads(project) == ["bbbb"])
    completed = _completed_up(project)
    assert completed[0] == 0, completed[1]
    assert _probe_counts(project) == [("aaaa", 1), ("bbbb", 1), ("cccc", 1)]
    assert _heads(project) == ["cccc"]


def test_version_table_failed_mutation_refuses_without_killing(project):
    _probe_revision(project, "aaaa", None)
    _probe_revision(project, "bbbb", "aaaa")
    first = project.run("up", "it", "-r", "aaaa")
    assert first.exit_code == 0, first.output
    project.client.command(
        f"ALTER TABLE {project.database}.alembic_version DELETE WHERE toUInt64(version_num) = 0",
        settings={"mutations_sync": 0},
    )
    _wait_for(lambda: bool(_failed_mutations(project)))
    failed_before = _failed_mutations(project)
    refused = _completed_up(project)
    assert refused[0] != 0
    assert "Version-table mutation" in refused[1] and "did not kill it" in refused[1]
    assert failed_before[0][1] in refused[1]
    assert _probe_counts(project) == [("aaaa", 1)]
    assert _failed_mutations(project)[0][0] == failed_before[0][0]


def test_version_table_atomic_cluster_is_shared_across_nodes(
    cluster_project, clickhouse_cluster, request
):
    project, cluster = cluster_project, clickhouse_cluster
    _bootstrap_cluster_user(project, cluster, request)
    project.write_revision("aaaa", {"upgrade": 'op.execute("SELECT 1")'})
    applied = project.run("up", "it")
    assert applied.exit_code == 0, applied.output
    _select_node(project, cluster, 2)
    _assert_one_applied(project)
    for client in cluster.clients.values():
        rows = client.query(
            "SELECT engine, total_replicas FROM system.replicas "
            "WHERE database = {db:String} AND table = 'alembic_version'",
            parameters={"db": project.database},
        ).result_rows
        assert rows == [("ReplicatedMergeTree", 2)]
        assert client.query(
            f"SELECT version_num FROM {project.database}.alembic_version"
        ).result_rows == [("aaaa",)]
        path = client.query(
            "SELECT zookeeper_path FROM system.replicas "
            "WHERE database = {db:String} AND table = 'alembic_version'",
            parameters={"db": project.database},
        ).result_rows[0][0]
        assert project.database in path


def test_version_table_replicated_database_uses_replicated_engine(
    cluster_project,
    clickhouse_cluster,
    request,
):
    project, cluster = cluster_project, clickhouse_cluster
    database = "it_rep_" + secrets.token_hex(8)
    for node, client in cluster.clients.items():
        request.addfinalizer(
            lambda owned=client: owned.command(f"DROP DATABASE IF EXISTS {database} SYNC")
        )
        client.command(
            f"CREATE DATABASE {database} ENGINE = Replicated('/clickhouse/databases/{database}', '01', 'r{node}') "
            "SETTINGS collection_name = 'chm_it_auth'"
        )
    config_path = project.root / "config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["environments"]["it"]["database"] = database
    config["environments"]["it"].pop("cluster")
    config_path.write_text(yaml.safe_dump(config))
    project.write_revision("aaaa", {"upgrade": 'op.execute("SELECT 1")'})
    applied = project.run("up", "it")
    assert applied.exit_code == 0, applied.output
    _select_node(project, cluster, 2)
    _assert_one_applied(project)
    for client in cluster.clients.values():
        assert client.query(
            "SELECT engine, total_replicas FROM system.replicas "
            "WHERE database = {db:String} AND table = 'alembic_version'",
            parameters={"db": database},
        ).result_rows == [("ReplicatedMergeTree", 2)]
        assert client.query(f"SELECT version_num FROM {database}.alembic_version").result_rows == [
            ("aaaa",)
        ]


def test_version_table_existing_unreplicated_cluster_state_warns_without_conversion(
    cluster_project,
    clickhouse_cluster,
):
    project = cluster_project
    project.client.command(
        f"CREATE TABLE {project.database}.alembic_version (version_num String) "
        "ENGINE = MergeTree ORDER BY version_num"
    )
    project.write_revision("aaaa", {"upgrade": 'op.execute("SELECT 1")'})
    project.client.command(f"INSERT INTO {project.database}.alembic_version VALUES ('aaaa')")
    upgraded = project.run("upgrade-env")
    assert upgraded.exit_code == 0, upgraded.output
    assert "not converted" in upgraded.output and "status <env>" in upgraded.output
    status = project.run("status", "it")
    assert status.exit_code == 0, status.output
    assert "non-replicated MergeTree" in status.output and "reconcile" in status.output
    assert project.client.query(
        "SELECT engine FROM system.tables WHERE database = {db:String} AND name = 'alembic_version'",
        parameters={"db": project.database},
    ).result_rows == [("MergeTree",)]


def test_version_table_failure_on_other_replica_refuses_up(cluster_project, clickhouse_cluster):
    project, cluster = cluster_project, clickhouse_cluster
    for client in cluster.clients.values():
        client.command(
            f"CREATE TABLE {project.database}.alembic_version (version_num String) "
            "ENGINE = MergeTree ORDER BY version_num"
        )
        client.command(f"INSERT INTO {project.database}.alembic_version VALUES ('aaaa')")
    project.write_revision("aaaa", {"upgrade": 'op.execute("SELECT 1")'})
    _probe_revision(project, "bbbb", "aaaa")
    cluster.clients[2].command(
        f"ALTER TABLE {project.database}.alembic_version DELETE WHERE toUInt64(version_num) = 0",
        settings={"mutations_sync": 0},
    )
    _wait_for(lambda: bool(_failed_mutations(project, cluster.clients[2])))
    refused = _completed_up(project)
    assert refused[0] != 0, refused
    assert "Version-table mutation" in refused[1] and cluster.hostnames[2] in refused[1]
    assert project.client.command(f"EXISTS TABLE {project.database}.probe") == 0
    assert _failed_mutations(project, cluster.clients[2])


def _probe_revision(project, revision, parent):
    project.write_revision(
        revision,
        {
            "upgrade": 'op.execute(f"CREATE TABLE IF NOT EXISTS {db}.probe (revision String) ENGINE = Memory")\n'
            "# ch-migrate: allow-non-idempotent Test counter exposes any repeated revision\n"
            f"op.execute(f\"INSERT INTO {{db}}.probe VALUES ('{revision}')\")"
        },
        down_revision=parent,
    )


def _start_up(project, extra=()):
    return subprocess.Popen(
        [sys.executable, "-m", "ch_migrate.cli", "up", "it", *extra],
        cwd=project.root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )


def _stop_process(process):
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
    try:
        return process.communicate(timeout=10)[0]
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        return process.communicate(timeout=5)[0]


def _completed_up(project, extra=()):
    process = _start_up(project, extra)
    try:
        output = process.communicate(timeout=20)[0]
        return process.returncode, output
    finally:
        _stop_process(process)


def _wait_for(predicate):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    pytest.fail("Version-table state did not reach the expected transition")


def _heads(project):
    return [
        row[0]
        for row in project.client.query(
            f"SELECT version_num FROM {project.database}.alembic_version ORDER BY version_num"
        ).result_rows
    ]


def _probe_counts(project):
    return project.client.query(
        f"SELECT revision, count() FROM {project.database}.probe GROUP BY revision ORDER BY revision"
    ).result_rows


def _wait_for_version_delete(project, process):
    deadline = time.monotonic() + 20
    rows = []
    while time.monotonic() < deadline:
        rows = project.client.query(
            "SELECT mutation_id, command, is_done, latest_fail_reason FROM system.mutations "
            "WHERE database = {db:String} AND table = 'alembic_version'",
            parameters={"db": project.database},
        ).result_rows
        if any(not done and "DELETE" in command.upper() for _, command, done, _ in rows):
            return {mutation for mutation, _, _, _ in rows}
        if process.poll() is not None:
            break
        time.sleep(0.05)
    output = _stop_process(process)
    pytest.fail(f"Pending version DELETE was not observed: {rows!r}; child output: {output}")


def _failed_mutations(project, client=None):
    client = client if client is not None else project.client
    return client.query(
        "SELECT mutation_id, latest_fail_reason FROM system.mutations "
        "WHERE database = {db:String} AND table = 'alembic_version' "
        "AND NOT is_done AND latest_fail_reason != '' ORDER BY mutation_id",
        parameters={"db": project.database},
    ).result_rows


def _bootstrap_cluster_user(project, cluster, request):
    monkeypatch = request.getfixturevalue("monkeypatch")
    user = project.database + "_migration"
    role = project.database + "_migration_role"
    monkeypatch.setenv("CH_IT_ADMIN_PASSWORD", cluster.servers[1].password)
    monkeypatch.setenv("CH_IT_MIGRATION_PASSWORD", secrets.token_hex(24))
    path = project.root / "config.yaml"
    config = yaml.safe_load(path.read_text())
    config["project"]["name"] = project.database
    config["environments"]["it"]["migration_user"] = user
    path.write_text(yaml.safe_dump(config))
    for node, client in cluster.clients.items():
        request.addfinalizer(lambda owned=client: owned.command(f"DROP ROLE IF EXISTS {role}"))
        request.addfinalizer(lambda owned=client: owned.command(f"DROP USER IF EXISTS {user}"))
        _select_node(project, cluster, node)
        result = project.run("bootstrap", "it")
        assert result.exit_code == 0, result.output
    _select_node(project, cluster, 1)


def _select_node(project, cluster, node):
    path = project.root / "config.yaml"
    config = yaml.safe_load(path.read_text())
    config["environments"]["it"].update(
        host=cluster.servers[node].host, port=cluster.servers[node].port
    )
    path.write_text(yaml.safe_dump(config))


def _assert_one_applied(project):
    status = project.run("status", "it")
    assert status.exit_code == 0, status.output
    assert re.search(r"Applied:\s+1\b", status.output), status.output
    assert re.search(r"Pending:\s+0\b", status.output), status.output
