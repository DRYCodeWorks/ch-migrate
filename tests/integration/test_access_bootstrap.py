"""`bootstrap` on the access engine, and `access check`, against real servers (design note, 2.5).

Every user and role a test creates starts with the project's `it_<hex>` name, so the access
fixtures' finalizer drops them; the cluster tests drop theirs on every node. What `bootstrap`
sent as the admin is recorded from the client, with the settings it was sent with, because
`wait_end_of_query` is an HTTP parameter that query_log doesn't keep.

Legacy mode, with no access.yaml, is pinned by `test_access_characterization.py`.
"""

import os
from dataclasses import replace

import pytest
from access_fixtures import new_access_project, read_access
from clickhouse_connect.driver.exceptions import DatabaseError
from test_access_apply import (
    assert_plan_in_sync,
    best_effort_held,
    cluster_project,
    drop_everywhere,
    logs_in,
    node_line,
)
from test_access_plan import access, show_access, write_access

pytestmark = pytest.mark.integration

CORE = "migration_role:\n  profile: core\n"
UPGRADE = "CREATE TABLE IF NOT EXISTS {db}.events (id UInt64) ENGINE = MergeTree ORDER BY id;\n"
DOWNGRADE = "DROP TABLE IF EXISTS {db}.events;\n"
# Core's best-effort grants as `best_effort_held` reads them, for a database.
EVERY_BEST_EFFORT = (
    ("CLUSTER", None, None),
    ("CREATE TEMPORARY TABLE", None, None),
    ("READ", None, None),
    ("SELECT", "system", "parts"),
    ("SELECT", "system", "replicas"),
    ("SYSTEM SYNC DATABASE REPLICA", "{db}", None),
    ("SYSTEM SYNC REPLICA", "{db}", None),
)
# What begins the line that names the node read on a local-storage cluster (Decision 14).
NAMED = "→ Read node "


# A new project (Decision 5).


def test_a_new_project_gets_core_and_its_migration_user_cannot_create_users(
    access_project, clickhouse_server, request
):
    """`init` writes access.yaml; `up` runs as the migration user, and CREATE USER doesn't."""
    project = access_project
    again = project.run("init", str(project.root), "--name", project.name)
    assert again.exit_code == 0 and "Created access.yaml" in again.stdout, again.output
    result = bootstrap(project)
    assert result.stdout.splitlines()[0].endswith("with the core profile"), result.output
    assert access(project, "plan", "it").exit_code == 0  # core is all it declares
    write_migration(project)
    assert project.run("up", "it").exit_code == 0
    assert project.client.command(f"EXISTS TABLE {project.database}.events") == 1
    login = migration_login(project, clickhouse_server, request)
    with pytest.raises(DatabaseError, match="ACCESS_DENIED"):
        login.command(f"CREATE USER {project.name}_extra IDENTIFIED BY 'Chm!not-made-1'")


# `access check`.


def test_check_passes_after_bootstrap_with_only_the_migration_users_secret(
    access_project, monkeypatch
):
    project = access_project
    write_access(project, CORE)
    bootstrap(project)
    monkeypatch.delenv("CH_IT_ADMIN_PASSWORD")
    result = access(project, "check", "it")
    assert (result.exit_code, result.stderr) == (0, ""), result.output
    assert result.stdout == (
        f"✓ user {project.migration_user} holds every required grant of role "
        f"{project.migration_role} (8).\n"
    )


def test_check_names_a_core_grant_revoked_by_hand(access_project):
    project = access_project
    write_access(project, CORE)
    bootstrap(project)
    project.client.command(f"REVOKE SELECT ON system.mutations FROM {project.migration_role}")
    result = access(project, "check", "it")
    assert result.exit_code == 1, result.output
    user, role = f"user {project.migration_user}", f"role {project.migration_role}"
    assert result.stderr.splitlines() == [
        f"✗ {user} lacks SELECT ON system.mutations",
        f"✗ {user} lacks 1 of the 8 required grants of {role}.",
    ]


def test_check_warns_about_a_missing_best_effort_grant(access_project):
    """READ ON REMOTE: CLUSTER can't be missing on ClickHouse Cloud, which gives it to all."""
    project = access_project
    write_access(project, CORE)
    bootstrap(project)
    project.client.command(f"REVOKE READ ON REMOTE FROM {project.migration_role}")
    result = access(project, "check", "it")
    assert result.exit_code == 0, result.output
    assert result.stderr == f"! best-effort: user {project.migration_user} lacks READ ON REMOTE\n"


# `bootstrap` with an access.yaml.


def test_bootstrap_makes_only_the_migration_identity_and_runs_again_cleanly(access_project):
    project, p = access_project, access_project.name
    write_access(project, CORE + f"roles:\n  {p}_analyst: {{grants: ['SELECT ON {{db}}.*']}}\n")
    result = bootstrap(project)
    lines = result.stdout.splitlines()
    assert lines[1:4] == [
        f"+ create database {project.database} if it doesn't exist",
        f"+ create role {project.migration_role}",
        f"+ create user {project.migration_user}",
    ]
    assert lines[-3:-1] == [
        f"✓ user {project.migration_user} holds every required grant of role "
        f"{project.migration_role} (8).",
        "✓ Bootstrap complete for it.",
    ]
    assert existing_roles(project) == [project.migration_role]  # `access apply` makes the rest
    again = bootstrap(project)
    assert [line[:2] for line in again.stdout.splitlines() if line[:2] in ("+ ", "- ")] == ["+ "]
    assert access(project, "plan", "it").exit_code == 1  # the analyst role is still pending


def test_bootstrap_with_the_smallest_admin_lands_every_required_grant(
    access_project, clickhouse_server, monkeypatch
):
    """T2's U6 admin, plus what reading access needs; best-effort shortfalls only warn."""
    from test_access_apply import READ_GRANTS, admin_login, use_admin
    from test_access_plan import core_best_effort
    from test_access_spike import SMALLEST_ADMIN

    project = access_project
    write_access(project, CORE)
    use_admin(
        project, admin_login(project, clickhouse_server, SMALLEST_ADMIN + READ_GRANTS), monkeypatch
    )
    result = bootstrap(project)
    warned = [line for line in result.stderr.splitlines() if line.startswith("! best-effort:")]
    # None of core's best-effort grants: CLUSTER only where the server needs it granted.
    assert len(warned) == len(core_best_effort(clickhouse_server)), result.output
    assert access(project, "check", "it").exit_code == 0


@pytest.mark.parametrize("declared", [True, False], ids=["access.yaml", "legacy"])
def test_bootstrap_refuses_24_8_before_any_write(old_clickhouse_server, request, declared):
    """Decision 11, legacy mode included: the version is named, nothing changes."""
    project = new_access_project(old_clickhouse_server, request)
    if declared:
        write_access(project, CORE)
    before = show_access(project)
    result = project.run("bootstrap", "it")
    assert result.exit_code == 2, result.output
    assert "The server runs ClickHouse 24.8." in result.stderr, result.output
    assert show_access(project) == before
    assert project.client.command(f"EXISTS DATABASE {project.database}") == 0


# `migration_role:` in config.yaml (2.8).


def test_legacy_bootstrap_skips_an_mcp_user_with_no_password(access_project, monkeypatch):
    """0.8 made the MCP user only when its password was set. 0.9 read
    it as required and stopped with exit 2 before making anything."""
    project = access_project
    monkeypatch.delenv("CH_IT_MCP_PASSWORD", raising=False)
    project.configure(mcp_user_name=project.name + "_mcp")
    result = project.run("bootstrap", "it")
    assert result.exit_code == 0, result.output
    assert "mcp_user_name names" in result.output and "skips that user" in result.output
    users = project.client.query(
        "SELECT name FROM system.users WHERE name LIKE {prefix:String} ORDER BY name",
        parameters={"prefix": project.name + "%"},
    ).result_rows
    assert users == [(project.migration_user,)]


def test_two_projects_share_one_migration_role(access_project, clickhouse_server, request):
    """Each directory's bootstrap adds core on its own database; neither revokes the other's."""
    first = access_project
    second = sharing_project(first, clickhouse_server, request)
    role = first.migration_role
    assert databases_granted(first.client, role) == {first.database, second.database}
    for project in (first, second):
        os.chdir(project.root)
        write_migration(project)
        assert project.run("up", "it").exit_code == 0
        version = f"{project.database}.alembic_version"
        assert project.client.command(f"SELECT count() FROM {version}") == 1
    before = read_access(first.client, [], [role])
    for again, other in ((first, second), (second, first)):
        os.chdir(again.root)
        bootstrap(again)
        assert read_access(first.client, [], [role]) == before
        os.chdir(other.root)
        assert access(other, "check", "it").exit_code == 0
    third = f"{first.name}_third"
    request.addfinalizer(lambda: first.client.command(f"DROP DATABASE IF EXISTS {third}"))
    first.client.command(f"CREATE DATABASE {third}")
    login = migration_login(first, clickhouse_server, request)
    with pytest.raises(DatabaseError, match="ACCESS_DENIED"):
        login.command(f"CREATE TABLE {third}.events (id UInt64) ENGINE = MergeTree ORDER BY id")


# Clusters.


@pytest.mark.cluster
def test_legacy_bootstrap_on_a_cluster_stays_on_one_node(clickhouse_cluster, monkeypatch, request):
    """Decision 13: no access.yaml, so no ON CLUSTER, and best-effort grants are CURRENT GRANTS
    on the node it is connected to. Each node is bootstrapped on its own, as in 0.8."""
    cluster = clickhouse_cluster
    project = bootstrap_cluster_project(cluster, request)
    sent = record_writes(monkeypatch)
    first = bootstrap(project)
    sqls = [sql for sql, _ in sent]
    assert sqls and not any("ON CLUSTER" in sql for sql in sqls), sqls
    assert not any(settings for _, settings in sent), sent
    role = project.migration_role
    assert f"GRANT CURRENT GRANTS(READ ON REMOTE) TO `{role}`" in sqls
    assert roles_on(cluster.clients[2], role) == []
    project.configure(port=cluster.servers[2].port)
    second = bootstrap(project)
    assert roles_on(cluster.clients[2], role) == [role]
    # Decision 14 is for `bootstrap` with an access.yaml: legacy mode names no node.
    assert not any(NAMED in result.output for result in (first, second))


@pytest.mark.cluster
def test_bootstrap_with_access_yaml_runs_on_cluster_and_reaches_every_node(
    clickhouse_cluster, monkeypatch, request
):
    """2.5 and Decision 12: every statement ON CLUSTER with wait_end_of_query=1, and each
    best-effort privilege its own plain GRANT ON CLUSTER."""
    cluster = clickhouse_cluster
    project = bootstrap_cluster_project(cluster, request)
    write_access(project, CORE)
    sent = record_writes(monkeypatch)
    bootstrap(project)
    assert sent and all(f" ON CLUSTER `{cluster.name}`" in sql for sql, _ in sent), sent
    assert all(settings.get("wait_end_of_query") == 1 for _, settings in sent), sent
    assert not any("CURRENT GRANTS" in sql for sql, _ in sent), sent
    expected = [
        tuple(part and part.format(db=project.database) for part in row)
        for row in EVERY_BEST_EFFORT
    ]
    for node in (1, 2):
        client = cluster.clients[node]
        assert client.command(f"EXISTS DATABASE {project.database}") == 1, node
        assert best_effort_held(client, project.migration_role, project.database) == expected, node
        project.configure(port=cluster.servers[node].port)
        assert access(project, "check", "it").exit_code == 0, node


@pytest.mark.cluster
def test_bootstrap_with_access_yaml_on_keeper_storage_needs_no_on_cluster(
    clickhouse_cluster_keeper, monkeypatch, request
):
    cluster = clickhouse_cluster_keeper
    project = bootstrap_cluster_project(cluster, request)
    write_access(project, CORE)
    sent = record_writes(monkeypatch)
    result = bootstrap(project)
    assert sent and not any("ON CLUSTER" in sql for sql, _ in sent), sent
    # Every replica is read at once, before and after the writes, so nothing waits (T8).
    assert "Reading again" not in result.stderr, result.stderr
    password = os.environ["CH_IT_MIGRATION_PASSWORD"]
    assert logs_in(cluster.servers[2], project.migration_user, password)
    # Decision 14: in Keeper, users and roles reach every node, so no node is named.
    results = [result, *(access(project, command, "it") for command in ("plan", "apply"))]
    assert [each.exit_code for each in results] == [0, 0, 0], [each.output for each in results]
    assert not any(NAMED in each.output for each in results), [each.output for each in results]


@pytest.mark.cluster
def test_on_local_storage_plan_apply_and_bootstrap_name_the_node_they_read(
    clickhouse_cluster, request
):
    """Decision 14: each reads only the node it connects to, and names it once, by hostName()."""
    cluster = clickhouse_cluster
    project = bootstrap_cluster_project(cluster, request)
    write_access(project, CORE)
    results = {"bootstrap": bootstrap(project)}
    results.update((command, access(project, command, "it")) for command in ("plan", "apply"))
    first = node_line(cluster.clients[1])
    for command, line in (("bootstrap", 1), ("plan", 0), ("apply", 0)):
        result = results[command]
        assert result.exit_code == 0, result.output
        named = [each for each in result.stdout.splitlines() if NAMED in each]
        assert named == [first] == [result.stdout.splitlines()[line]], (command, result.output)
    second = node_line(cluster.clients[2])
    assert second != first
    project.configure(port=cluster.servers[2].port)
    assert_plan_in_sync(project, cluster.clients[2])


# Helpers.


def bootstrap(project):
    """Run `bootstrap it`, which must exit 0."""
    result = project.run("bootstrap", "it")
    assert result.exit_code == 0, result.output
    assert "stopped part-way" not in result.stderr, result.output
    return result


def sharing_project(first, server, request):
    """`first` bootstrapped with core, then a second directory that shares its role and user.

    The second is bootstrapped too, and is left as the working directory.
    """
    password = os.environ["CH_IT_MIGRATION_PASSWORD"]
    write_access(first, CORE)
    bootstrap(first)
    second = new_access_project(server, request, first.root / "second")
    request.getfixturevalue("monkeypatch").setenv("CH_IT_MIGRATION_PASSWORD", password)
    second.configure(migration_user=first.migration_user, migration_role=first.migration_role)
    write_access(second, CORE)
    bootstrap(second)
    return second


def migration_login(project, server, request):
    password = os.environ["CH_IT_MIGRATION_PASSWORD"]
    client = replace(server, user=project.migration_user, password=password).connect()
    request.addfinalizer(client.close)
    return client


def bootstrap_cluster_project(cluster, request):
    """`cluster_project`, with no access.yaml, whose database is dropped on every node."""
    project = cluster_project(cluster, request)
    (project.root / "access.yaml").unlink()  # `init` writes one for a new project
    database = project.database
    request.addfinalizer(lambda: drop_databases(cluster, database))
    request.addfinalizer(lambda: drop_everywhere(cluster, project.name))
    return project


def drop_databases(cluster, database):
    for client in cluster.clients.values():
        client.command(f"DROP DATABASE IF EXISTS {database} SYNC")


def record_writes(monkeypatch):
    """Record each statement `bootstrap` sends as the admin, with the settings it sends it with."""
    from ch_migrate.access import bootstrap as engine

    sent = []
    opened = engine.open_client

    def recording(project_settings, login):
        client = opened(project_settings, login)
        if login.user != project_settings.admin_user:
            return client
        command = client.command

        def recorded(sql, parameters=None, settings=None):
            if not sql.startswith("CHECK GRANT"):  # a read, as of whether CLUSTER is everyone's
                sent.append((sql, dict(settings or {})))
            return command(sql, parameters=parameters, settings=settings)

        client.command = recorded
        return client

    monkeypatch.setattr(engine, "open_client", recording)
    return sent


def write_migration(project):
    result = project.run("new", "it", "add_events")
    assert result.exit_code == 0, result.output
    [up] = list((project.root / "migrations" / "sql").rglob("*.up.sql"))
    [down] = list((project.root / "migrations" / "sql").rglob("*.down.sql"))
    up.write_text(UPGRADE)
    down.write_text(DOWNGRADE)


def existing_roles(project):
    rows = project.client.query(
        "SELECT name FROM system.roles WHERE startsWith(name, {p:String}) ORDER BY name",
        parameters={"p": project.name},
    ).result_rows
    return [name for (name,) in rows]


def roles_on(client, role):
    rows = client.query(
        "SELECT name FROM system.roles WHERE name = {r:String}", parameters={"r": role}
    ).result_rows
    return [name for (name,) in rows]


def databases_granted(client, role):
    rows = client.query(
        "SELECT DISTINCT database FROM system.grants "
        "WHERE role_name = {r:String} AND access_type = 'CREATE TABLE'",
        parameters={"r": role},
    ).result_rows
    return {database for (database,) in rows}
