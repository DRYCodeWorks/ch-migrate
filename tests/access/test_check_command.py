"""`access check` without a server: what it checks, as whom, and how it exits (2.5).

The migration user's connection is a `FakeServer` that answers each `CHECK GRANT`. Statements
are checked by text here; `tests/integration/test_access_bootstrap.py` runs them.
"""

import pytest
from click.testing import CliRunner
from fake_access_server import FakeServer

from ch_migrate.cli import main

CORE_REQUIRED = 8
CORE_BEST_EFFORT = 6


def run(*args):
    result = CliRunner().invoke(main, ["access", *args])
    assert result.exception is None or isinstance(result.exception, SystemExit), result.output
    return result


@pytest.fixture
def server(project, monkeypatch):
    """The migration user's connection; records who logged in, through `connect`."""
    fake = FakeServer()
    fake.logins = []

    def open_client(settings, login):
        fake.logins.append(login)
        return fake

    monkeypatch.setattr("ch_migrate.access.server.open_client", open_client)
    return fake


def core(project, extra=""):
    (project / "access.yaml").write_text("migration_role:\n  profile: core\n" + extra)


def test_every_grant_held_exits_0_as_the_migration_user(project, server):
    core(project)
    result = run("check", "dev")
    assert (result.exit_code, result.stderr) == (0, ""), result.output
    assert result.stdout == (
        "✓ user shop_migrator holds every required grant of role shop_migration_role (8).\n"
    )
    [login] = server.logins
    assert (login.user, login.secret) == ("shop_migrator", "migration_password")
    assert len(server.checks) == CORE_REQUIRED + CORE_BEST_EFFORT
    assert server.writes == [] and server.closed


def test_each_grant_is_checked_as_its_flags_on_its_target(project, server):
    """CHECK GRANT on a group checks flags its target can't hold, so leaves are sent."""
    run("check", "dev")  # legacy mode: no access.yaml
    assert (
        "CHECK GRANT ALTER ROW POLICY, CREATE ROW POLICY, DROP ROW POLICY, SHOW ROW POLICIES "
        "ON `shop_dev`.*"
    ) in server.checks
    assert "CHECK GRANT READ ON REMOTE" in server.checks
    assert not any("GRANT OPTION" in sql for sql in server.checks)


def test_a_missing_required_grant_exits_1_naming_it(project, server):
    core(project)
    server.lacking = {"`system`.`mutations`"}
    result = run("check", "dev")
    assert result.exit_code == 1, result.output
    assert result.stdout == ""
    assert result.stderr.splitlines() == [
        "✗ user shop_migrator lacks SELECT ON system.mutations",
        "✗ user shop_migrator lacks 1 of the 8 required grants of role shop_migration_role.",
    ]


def test_a_missing_best_effort_grant_warns_and_exits_0(project, server):
    core(project)
    server.lacking = {"CLUSTER ON"}
    result = run("check", "dev")
    assert result.exit_code == 0, result.output
    assert result.stderr == "! best-effort: user shop_migrator lacks CLUSTER ON *.*\n"


def test_migration_role_grants_are_checked_too(project, server):
    core(project, "  grants:\n    - CREATE FUNCTION ON *.*\n")
    server.lacking = {"CREATE FUNCTION"}
    result = run("check", "dev")
    assert result.exit_code == 1, result.output
    assert "✗ user shop_migrator lacks CREATE FUNCTION ON *.*" in result.stderr


def test_a_server_older_than_25_8_exits_2_naming_its_version(project, server):
    core(project)
    server.server_version = "24.8.14.39"
    result = run("check", "dev")
    assert result.exit_code == 2, result.output
    assert "ClickHouse 24.8.14.39" in result.stderr
    assert server.checks == []
