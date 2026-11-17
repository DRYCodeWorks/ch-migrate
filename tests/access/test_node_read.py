"""Which node `access plan`, `access apply` and `bootstrap` read, without a server (Decision 14).

On a cluster whose users and roles are on each node's own disk, they read only the node they
connect to, so each prints one line naming it. The admin's connection and the migration
user's are one `FakeServer`. `tests/integration/test_access_bootstrap.py` runs the same
commands on the local-storage and Keeper cluster fixtures.
"""

import pytest
from click.testing import CliRunner
from fake_access_server import FakeServer

from ch_migrate.access.loader import validate_access
from ch_migrate.access.settings import load_settings
from ch_migrate.cli import main

CORE = "migration_role:\n  profile: core\n"
PASSWORDS = {"ADMIN": "Adm!" + "3c" * 12, "MIGRATION": "Migr!" + "2b" * 12}
NAMED = (
    "→ Read node {} only: users and roles are on each node's own disk. After a failed "
    "ON CLUSTER apply, run ch-migrate access plan dev against each node."
)
# Each command, and the line of its stdout that names the node: `bootstrap` heads its own.
COMMANDS = [
    (("access", "plan", "dev"), 0),
    (("access", "apply", "dev"), 0),
    (("bootstrap", "dev"), 1),
]


def run(*args):
    result = CliRunner().invoke(main, list(args))
    assert result.exception is None or isinstance(result.exception, SystemExit), result.output
    return result


@pytest.fixture
def server(project, monkeypatch):
    """One server for every login, on each node's own disk, with core declared; secrets set."""
    (project / "access.yaml").write_text(CORE)
    fake = FakeServer(storage="local_directory")
    fake.node = "ch-node-1"
    monkeypatch.setattr("ch_migrate.access.cli.connect_admin", lambda settings: fake)
    monkeypatch.setattr("ch_migrate.access.bootstrap.open_client", lambda settings, login: fake)
    for name, value in PASSWORDS.items():
        monkeypatch.setenv(f"CH_DEV_{name}_PASSWORD", value)
    return fake


def set_config(project, *lines):
    """Add lines under the `dev` environment in config.yaml."""
    path = project / "config.yaml"
    added = "".join(f"{line}\n" for line in lines)
    path.write_text(
        path.read_text().replace("    host: localhost\n", f"    host: localhost\n{added}", 1)
    )


def named(result):
    return [line for line in result.stdout.splitlines() if line.startswith("→ Read node")]


@pytest.mark.parametrize(("command", "line"), COMMANDS)
def test_a_local_storage_cluster_names_the_node_read_once(project, server, command, line):
    set_config(project, "    cluster: shop_cluster")
    result = run(*command)
    assert named(result) == [NAMED.format("ch-node-1")], result.output
    assert result.stdout.splitlines()[line] == NAMED.format("ch-node-1")


def test_a_node_that_wont_say_its_name_is_named_by_host_and_port(project, server):
    set_config(project, "    cluster: shop_cluster", "    port: 9123")
    server.node = None
    result = run("access", "plan", "dev")
    assert named(result) == [NAMED.format("localhost:9123")], result.output


@pytest.mark.parametrize(("command", "line"), COMMANDS)
@pytest.mark.parametrize(
    ("storage", "config"),
    [
        ("replicated", ("    cluster: shop_cluster", "    access_read_timeout: 0")),
        ("local_directory", ()),
    ],
    ids=["keeper", "single node"],
)
def test_keeper_storage_and_a_single_node_name_no_node(
    project, server, command, line, storage, config
):
    """The server already holds core, so each command runs to its end and exits 0."""
    server.storage = storage
    set_config(project, *config)
    spec = validate_access(load_settings("dev", project / "config.yaml"), project).spec
    server.hold(spec)
    server.converges_to = spec
    result = run(*command)
    assert result.exit_code == 0, result.output
    assert named(result) == [], result.output


def test_legacy_bootstrap_on_a_cluster_names_no_node(project, server):
    """Decision 13: with no access.yaml, `bootstrap` runs on its node as 0.8 does."""
    (project / "access.yaml").unlink()
    set_config(project, "    cluster: shop_cluster")
    result = run("bootstrap", "dev")
    assert result.exit_code == 0, result.output
    assert named(result) == [], result.output
