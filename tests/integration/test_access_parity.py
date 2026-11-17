"""Access where servers differ from a stock local one, as ClickHouse Cloud does (T8, 2.5).

- **Every replica at once.** Where users and roles are in Keeper, `plan`, `apply` and
  `bootstrap` read every replica in one query per table, so a role missing on every replica
  is absent at once. When the replicas can't be read that way, they read the node they are
  connected to and read again for what is missing, saying why only when they wait.
- **CLUSTER for every login.** ClickHouse Cloud grants CLUSTER to every login, so the
  best-effort `CLUSTER ON *.*` is already held there, and `plan`, `apply`, `check` and
  `bootstrap` agree. Where ON CLUSTER needs the grant, a missing one is still a warning.
- **A read-only directory first.** ClickHouse Cloud lists `jwt` before `replicated`. A
  create goes to the first directory that can take one, and that is the storage read.

The Keeper tests run in the cluster suite, on the owned two-node cluster. The others run on
ClickHouse Cloud itself, or on a container of their own configured as Cloud is.
"""

import pytest
from access_fixtures import new_access_project, requires_cluster_grant
from test_access_apply import READ_GRANTS, admin_login, cluster_project, use_admin
from test_access_plan import access, marked, write_access
from test_access_spike import SMALLEST_ADMIN

from ch_migrate.access.live import Wanted, read_live

pytestmark = pytest.mark.integration

ONE_ROLE = "roles: {'{project}_a': {}}\n"
CORE = "migration_role:\n  profile: core\n"
WAITING = (
    "→ Something isn't on the server yet. Reading again for up to 0.3 s, "
    "in case a replica is behind."
)
FALLING_BACK = (
    "→ Can't read every replica at once, so a missing row is read again for up to 0.3 s. "
    "Reading every replica of cluster {cluster} failed: "
)


@pytest.mark.cluster
def test_keeper_storage_without_cluster_set_reads_every_replica(clickhouse_cluster_keeper, request):
    """The one cluster holding this node and another host is found. The stock `default`
    cluster, which lists only localhost, isn't one, so it doesn't count."""
    project = cluster_project(clickhouse_cluster_keeper, request)
    project.configure(cluster=None)
    write_access(project, ONE_ROLE)
    result = access(project, "plan", "it")
    assert result.exit_code == 1, result.output
    assert f"create role {project.name}_a" in marked(result, "+")
    assert announced(result) == [], result.output


@pytest.mark.cluster
def test_a_configured_cluster_always_wins(clickhouse_cluster_keeper, request, monkeypatch):
    """An admin that can't read system.clusters still reads every replica through the
    configured cluster. Without it set, the same admin can't look for one, and says so."""
    cluster = clickhouse_cluster_keeper
    project = cluster_project(cluster, request)
    project.configure(access_read_timeout=0.3)
    reading = tuple(grant for grant in READ_GRANTS if "system.clusters" not in grant)
    login = admin_login(project, cluster.servers[1], SMALLEST_ADMIN + reading)
    use_admin(project, login, monkeypatch)
    write_access(project, ONE_ROLE)
    result = access(project, "plan", "it")
    assert result.exit_code == 1, result.output
    assert announced(result) == [], result.output
    project.configure(cluster=None)
    result = access(project, "plan", "it")
    assert result.exit_code == 1, result.output
    assert any(
        f"Can't read system.clusters: {login.user}: Not enough privileges." in notice
        for notice in announced(result)
    ), result.stderr


@pytest.mark.cluster
def test_an_admin_without_read_on_remote_reads_its_node_and_says_why(
    clickhouse_cluster_keeper, request, monkeypatch
):
    """Without READ ON REMOTE the reader retries missing rows on its own node, explaining
    the fallback once when it actually waits."""
    cluster = clickhouse_cluster_keeper
    project = cluster_project(cluster, request)
    project.configure(access_read_timeout=0.3)
    reading = tuple(grant for grant in READ_GRANTS if "REMOTE" not in grant)
    login = admin_login(project, cluster.servers[1], SMALLEST_ADMIN + reading)
    use_admin(project, login, monkeypatch)
    write_access(project, ONE_ROLE)
    result = access(project, "plan", "it")
    assert result.exit_code == 1, result.output
    assert announced(result) == [
        WAITING,
        FALLING_BACK.format(cluster=cluster.name)
        + f"{login.user}: Not enough privileges. To execute this query, it's necessary to "
        "have the grant READ ON REMOTE. (ACCESS_DENIED)",
    ]


@pytest.mark.cluster
def test_a_replica_that_cannot_be_reached_makes_the_reader_fall_back(
    clickhouse_cluster_keeper, request
):
    """A replica that is down fails the read of every replica; it never reads as empty."""
    cluster = clickhouse_cluster_keeper
    project = cluster_project(cluster, request)
    project.configure(access_read_timeout=0.3)
    write_access(project, ONE_ROLE)
    cluster.stop_node(2)
    try:
        result = access(project, "plan", "it")
    finally:
        cluster.start_node(2)
    assert result.exit_code == 1, result.output
    assert f"create role {project.name}_a" in marked(result, "+")
    notices = announced(result)
    # Trying the replica that is down may consume the deadline, leaving no actual wait.
    if notices:
        [waiting, fallback] = notices
        assert waiting == WAITING
        assert fallback.startswith(FALLING_BACK.format(cluster=cluster.name)), fallback
        assert fallback.endswith(" (ALL_CONNECTION_TRIES_FAILED)"), fallback


@pytest.mark.parametrize("server", ["implicit_cluster_server", "clickhouse_server"])
def test_plan_apply_check_and_bootstrap_agree_on_cluster(request, monkeypatch, server):
    """Where every login holds CLUSTER, nothing lacks it: no warning, and no best-effort try
    on each `apply`. Where ON CLUSTER needs the grant, an admin without it can't give it,
    and each command warns. Same admin, same project, on both kinds of server."""
    server = request.getfixturevalue(server)
    project = new_access_project(server, request)
    use_admin(project, admin_login(project, server, SMALLEST_ADMIN + READ_GRANTS), monkeypatch)
    write_access(project, CORE)
    results = {
        "bootstrap": project.run("bootstrap", "it"),
        "plan": access(project, "plan", "it"),
        "apply": access(project, "apply", "it"),
        "check": access(project, "check", "it"),
    }
    lacking = requires_cluster_grant(server)
    for command, result in results.items():
        assert result.exit_code == 0, (command, result.output)
        warned = "lacks CLUSTER ON *.*" in result.stderr
        tried = "as much of CLUSTER ON *.* as the admin holds" in result.output
        assert (warned, tried) == (lacking, lacking and command != "plan" and command != "check"), (
            command,
            result.output,
        )


def test_a_create_goes_to_the_first_directory_that_can_take_one(
    read_only_directory_server, request
):
    """The first directory after users.xml only reads: `ldap` here, `jwt` on Cloud. The one
    a create goes to is the storage read, so `plan` doesn't wait for a role nobody holds."""
    project = new_access_project(read_only_directory_server, request)
    client = project.client
    directories = client.query(
        "SELECT name, type FROM system.user_directories ORDER BY precedence"
    ).result_rows
    assert directories[1][1] in ("ldap", "jwt"), directories
    client.command(f"CREATE ROLE {project.name}_made")
    stored = client.command(
        "SELECT storage FROM system.roles WHERE name = {name:String}",
        parameters={"name": f"{project.name}_made"},
    )
    assert read_live(client, Wanted(())).storage == dict(directories)[stored]
    write_access(project, ONE_ROLE)
    project.configure(access_read_timeout=0.3)
    result = access(project, "plan", "it")
    assert result.exit_code == 1, result.output
    assert announced(result) == [], result.output


def announced(result):
    """The `→` lines on stderr: a wait, or why every replica can't be read."""
    return [line for line in result.stderr.splitlines() if line.startswith("→ ")]
