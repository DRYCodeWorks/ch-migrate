"""`access plan` and `access show` without a server: reads after writes, and exit codes.

The admin's connection is a `FakeServer`. A read that finds nothing is read again before
anything counts as absent (design note, 2.5), and whatever stops a live command exits 2,
because exit 1 means "changes pending".
"""

import click
import pytest
from click.testing import CliRunner
from fake_access_server import FakeServer, grant_rows

from ch_migrate import ui
from ch_migrate.access.grants import parse_grant
from ch_migrate.access.loader import validate_access
from ch_migrate.access.server import AccessError
from ch_migrate.access.settings import load_settings
from ch_migrate.cli import main

ACCESS_YAML = """\
roles:
  analyst: {grants: ['SELECT, SHOW TABLES ON {db}.*']}
users:
  dashboards: {password: {secret: dashboards_password}, roles: [analyst]}
"""
IN_SYNC = "✓ In sync for dev (access.yaml): nothing to change.\n"
WAITING = (
    "→ Something isn't on the server yet. Reading again for up to {seconds} s, "
    "in case a replica is behind.\n"
)
FALLING_BACK = (
    "→ Can't read every replica at once, so a missing row is read again for up to "
    "{seconds} s. {reason}\n"
)


def run(*args):
    result = CliRunner().invoke(main, ["access", *args])
    assert result.exception is None or isinstance(result.exception, SystemExit), result.output
    return result


@pytest.fixture
def server(project, monkeypatch):
    """A server that holds everything ACCESS_YAML declares, in Keeper storage."""
    (project / "access.yaml").write_text(ACCESS_YAML)
    settings = load_settings("dev", project / "config.yaml")
    fake = FakeServer()
    fake.hold(validate_access(settings, project).spec)
    monkeypatch.setattr("ch_migrate.access.cli.connect_admin", lambda settings: fake)
    return fake


def drop_cluster(server):
    """The server holds no CLUSTER grant for anyone."""
    server.tables["grants"] = [
        row for row in server.tables["grants"] if row["access_type"] != "CLUSTER"
    ]


def not_read_again(result):
    """Neither a wait nor a fallback was announced: best-effort warnings may be there."""
    return all(line.startswith("! best-effort: ") for line in result.stderr.splitlines())


def set_read_timeout(project, seconds):
    config = (project / "config.yaml").read_text()
    (project / "config.yaml").write_text(
        config.replace(
            "    host: localhost\n", f"    host: localhost\n    access_read_timeout: {seconds}\n"
        )
    )


@pytest.mark.parametrize(
    "clusters",
    [None, [], [{"cluster": "default", "hosts": ["localhost:9000"]}]],
    ids=["every replica", "no cluster", "a lone replicated node"],
)
def test_in_sync_reads_once(server, clusters):
    if clusters is not None:
        server.clusters = clusters
    result = run("plan", "dev")
    assert (result.exit_code, result.stdout, result.stderr) == (0, IN_SYNC, "")
    assert server.reads.count("roles") == 1 and server.closed


def test_every_replica_is_read_at_once_so_what_none_holds_is_absent_at_once(server):
    """Missing on every replica, a role is absent without a wait (design note, 2.5)."""
    server.empty_reads = 1
    result = run("plan", "dev")
    assert result.exit_code == 1, result.output
    assert "+ create role analyst" in result.stdout.splitlines()
    assert not_read_again(result) and server.reads.count("roles") == 1
    assert server.replica_settings[-1] == {"skip_unavailable_shards": 0}


def test_replicas_that_disagree_are_read_again_until_they_agree(server):
    """One replica shows nothing on the first read: `plan` waits, then reports in sync. The
    first read also read the node connected to, as replicas that disagree do."""
    server.behind = {"node-2": 1}
    result = run("plan", "dev")
    assert (result.exit_code, result.stdout) == (0, IN_SYNC), result.output
    assert result.stderr == WAITING.format(seconds=30)
    assert server.reads.count("roles") == 3


def test_replicas_that_still_disagree_at_the_deadline_leave_the_node_connected_to(server, project):
    """No row counts as present or absent from replicas that disagree. At
    the deadline the node connected to counts, as when only it is read, and one line names
    the replicas. Here that node holds everything, so `plan` is in sync."""
    set_read_timeout(project, 0.2)
    server.behind = {"node-2": 1000}
    result = run("plan", "dev")
    assert (result.exit_code, result.stdout) == (0, IN_SYNC), result.output
    assert result.stderr == WAITING.format(seconds=0.2) + (
        "→ Replicas node-1 and node-2 still disagree after 0.2 s, so what the node connected "
        "to holds counts.\n"
    )
    assert server.reads.count("roles") >= 4


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (
            {
                "replica_error": "Code: 497. DB::Exception: shop_admin: Not enough privileges. "
                "To execute this query, it's necessary to have the grant READ ON REMOTE. "
                "(ACCESS_DENIED) (version 26.3.1.1 (official build)) (for url http://db:8123)"
            },
            "Reading every replica of cluster default failed: shop_admin: Not enough "
            "privileges. To execute this query, it's necessary to have the grant READ ON "
            "REMOTE. (ACCESS_DENIED)",
        ),
        (
            {"clusters": []},
            "No cluster in system.clusters holds this node and another host. "
            "If other nodes share these users and roles, set cluster in config.yaml "
            "to a cluster that lists them.",
        ),
        (
            {"clusters": [{"cluster": "default", "hosts": ["localhost:9000"]}]},
            "No cluster in system.clusters holds this node and another host. "
            "If other nodes share these users and roles, set cluster in config.yaml "
            "to a cluster that lists them.",
        ),
        (
            {
                "clusters": [
                    {"cluster": "a", "hosts": ["n1:9000", "n2:9000"]},
                    {"cluster": "b", "hosts": ["n1:9000", "n3:9000"]},
                ]
            },
            "Clusters a and b hold this node and list different hosts. "
            "Set cluster in config.yaml to the cluster whose nodes share these users and roles.",
        ),
    ],
    ids=["no READ ON REMOTE", "no cluster", "a one-host cluster", "two clusters"],
)
def test_when_every_replica_cant_be_read_the_node_is_read_again(server, change, reason):
    """The reader says why once when an empty read makes it wait, then reports in sync."""
    for key, value in change.items():
        setattr(server, key, value)
    server.empty_reads = 1
    result = run("plan", "dev")
    assert (result.exit_code, result.stdout) == (0, IN_SYNC), result.output
    assert result.stderr == WAITING.format(seconds=30) + FALLING_BACK.format(
        seconds=30, reason=reason
    )
    assert server.reads.count("roles") == 2


TWO_HOSTS = ["node-1:9000", "node-2:9000"]


@pytest.mark.parametrize(
    "clusters",
    [
        [{"cluster": "main", "hosts": TWO_HOSTS}],
        # ClickHouse Cloud's `default` and `all_groups.default` list the same hosts.
        [{"cluster": "a", "hosts": TWO_HOSTS}, {"cluster": "b", "hosts": TWO_HOSTS}],
        # A stock image's `default` lists localhost alone, beside the real cluster.
        [
            {"cluster": "default", "hosts": ["localhost:9000"]},
            {"cluster": "main", "hosts": TWO_HOSTS},
        ],
    ],
    ids=["one cluster", "two with the same hosts", "a one-host default beside it"],
)
def test_the_one_cluster_holding_this_node_and_another_host_is_read_through(server, clusters):
    """Every replica is read through the first by name of the clusters that list the same
    hosts, so a role missing on all of them is absent at once."""
    server.clusters = clusters
    server.empty_reads = 1
    result = run("plan", "dev")
    assert result.exit_code == 1, result.output
    assert not_read_again(result) and server.reads.count("roles") == 1
    through = [name for name in ("main", "a") if any(c["cluster"] == name for c in clusters)]
    assert {parameters["cluster"] for parameters in server.replica_parameters} == set(through)


def test_the_configured_cluster_is_read_through_as_given(server, project):
    """A configured cluster always wins: no cluster is looked for, even where none would be
    found. A replica that can't be reached through it is a fallback, never a failure."""
    config = project / "config.yaml"
    config.write_text(
        config.read_text().replace(
            "    host: localhost\n", "    host: localhost\n    cluster: main\n"
        )
    )
    server.clusters = []
    assert run("plan", "dev").stderr == ""
    assert "clusters" not in server.reads
    assert {parameters["cluster"] for parameters in server.replica_parameters} == {"main"}
    server.replica_error = (
        "Code: 279. DB::Exception: All connection tries failed. (ALL_CONNECTION_TRIES_FAILED)"
    )
    result = run("plan", "dev")
    assert (result.exit_code, result.stdout) == (0, IN_SYNC), result.output
    assert result.stderr == ""


def test_a_read_that_finds_nothing_is_read_again_before_it_counts_as_absent(server):
    """Reading one node: one empty read, then the real rows. `plan` reports in sync, not 9
    creates and grants."""
    server.clusters = []
    server.empty_reads = 1
    result = run("plan", "dev")
    assert (result.exit_code, result.stdout) == (0, IN_SYNC), result.output
    assert WAITING.format(seconds=30) in result.stderr
    assert server.reads.count("roles") == 2


def test_what_is_still_missing_at_the_time_limit_is_absent(server, project):
    set_read_timeout(project, 0.2)
    server.clusters = []
    server.empty_reads = 1000
    result = run("plan", "dev")
    assert result.exit_code == 1, result.output
    assert "+ create role analyst" in result.stdout.splitlines()
    assert WAITING.format(seconds=0.2) in result.stderr
    assert server.reads.count("roles") >= 3


def test_a_time_limit_of_0_reads_once(server, project):
    set_read_timeout(project, 0)
    server.clusters = []  # Even a fallback stays quiet when no wait is allowed.
    server.empty_reads = 1
    result = run("plan", "dev")
    assert result.exit_code == 1, result.output
    assert server.reads.count("roles") == 1 and not_read_again(result)


def test_a_nodes_own_disk_is_never_behind_so_it_is_read_once(server):
    server.storage = "local_directory"
    server.empty_reads = 1
    result = run("plan", "dev")
    assert result.exit_code == 1, result.output
    assert server.reads.count("roles") == 1 and "Reading again" not in result.stderr


def test_a_login_that_cannot_read_the_storage_is_read_again(server):
    """Not knowing where users and roles are stored, `plan` takes it that a replica may lag."""
    server.storage = None
    server.empty_reads = 1
    result = run("plan", "dev")
    assert (result.exit_code, result.stdout) == (0, IN_SYNC), result.output
    assert server.reads.count("roles") == 2


def test_a_missing_best_effort_grant_is_not_read_again(server):
    """It is a warning and leads to no create or grant, so there is nothing to wait for."""
    drop_cluster(server)
    result = run("plan", "dev")
    assert (result.exit_code, result.stdout) == (0, IN_SYNC), result.output
    assert result.stderr == "! best-effort: role shop_migration_role lacks CLUSTER ON *.*\n"
    assert server.reads.count("roles") == 1


def test_cluster_granted_to_every_login_is_held(server):
    """As on ClickHouse Cloud: the admin's CHECK GRANT CLUSTER answers 1 although no grant of
    its own holds it, so the migration role lacks nothing, and nothing is read again."""
    drop_cluster(server)
    server.own_grants = []
    result = run("plan", "dev")
    assert (result.exit_code, result.stdout, result.stderr) == (0, IN_SYNC, ""), result.output
    assert server.reads.count("roles") == 1


@pytest.mark.parametrize("held", ["ALL ON *.* WITH GRANT OPTION", "CLUSTER ON *.*"])
def test_cluster_the_admin_holds_by_a_grant_is_not_granted_to_all(server, held):
    """`CHECK GRANT` answers 1 for an admin that holds CLUSTER by a grant, too, so a role
    without CLUSTER still lacks it, where the server needs the grant."""
    drop_cluster(server)
    server.own_grants = grant_rows(parse_grant(held))
    result = run("plan", "dev")
    assert (result.exit_code, result.stdout) == (0, IN_SYNC), result.output
    assert result.stderr == "! best-effort: role shop_migration_role lacks CLUSTER ON *.*\n"


def test_show_reads_again_for_a_name_it_cannot_find(server):
    server.behind = {"node-1": 1}
    result = run("show", "dev", "analyst")
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[3:] == [
        "roles:",
        "  analyst:",
        "    grants:",
        "      - SELECT, SHOW TABLES ON {db}.*",
    ]
    assert result.stderr == WAITING.format(seconds=30)
    assert server.reads.count("roles") == 3 and server.closed


@pytest.mark.parametrize("command", [("plan", "dev"), ("show", "dev")])
@pytest.mark.parametrize(
    ("error", "printed"),
    [
        (RuntimeError("boom"), "✗ RuntimeError: boom\n"),
        # click would exit 1 for these on its own.
        (ui.Failure("No such thing.", "Try `ch-migrate status dev`."), None),
        (click.ClickException("refused"), "Error: refused\n"),
    ],
)
def test_an_error_that_stops_a_live_command_exits_2_never_1(
    server, monkeypatch, command, error, printed
):

    def broken(*args):
        raise error

    monkeypatch.setattr("ch_migrate.access.live.Reader.read", broken)
    result = run(*command)
    assert (result.exit_code, result.stdout) == (2, ""), result.output
    assert result.stderr == (printed or "✗ No such thing.\nTry ch-migrate status dev.\n")
    assert server.closed


def test_an_access_error_prints_its_message_and_exits_2(project, monkeypatch):
    def refuse(settings):
        raise AccessError("The server runs ClickHouse 24.8.14.39.")

    monkeypatch.setattr("ch_migrate.access.cli.connect_admin", refuse)
    for command in ("plan", "show"):
        result = run(command, "dev")
        assert (result.exit_code, result.stdout) == (2, "")
        assert result.stderr == "✗ The server runs ClickHouse 24.8.14.39.\n"


def test_an_unknown_environment_exits_2(project):
    result = run("plan", "nowhere")
    assert (result.exit_code, result.stdout) == (2, ""), result.output
    assert result.stderr.startswith("✗ Unknown environment: nowhere.")
