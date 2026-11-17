"""`ch-migrate bootstrap` without a server: what it sends, as whom, and how it exits (2.5).

The admin's and the migration user's connections are `FakeServer`s, opened through
`open_client`. Exit 2 means that nothing was written, so every exit-2 test also checks that
no write was sent. `tests/integration/test_access_bootstrap.py` runs the statements.
"""

import os
import socket

import pytest
from click.testing import CliRunner
from fake_access_server import FakeServer

from ch_migrate.access.loader import validate_access
from ch_migrate.access.planner import Plan
from ch_migrate.access.settings import load_settings
from ch_migrate.cli import main

CORE = "migration_role:\n  profile: core\n"
DENIED = "Code: 497. Not enough privileges. (ACCESS_DENIED)"
DECLARED = CORE + "roles:\n  analyst: {grants: ['SELECT ON {db}.*']}\n"
PASSWORDS = {"ADMIN": "Adm!" + "3c" * 12, "MIGRATION": "Migr!" + "2b" * 12}


def bootstrap(*args):
    result = CliRunner().invoke(main, ["bootstrap", *args])
    assert result.exception is None or isinstance(result.exception, SystemExit), result.output
    return result


@pytest.fixture
def servers(project, monkeypatch):
    """The admin's server and the migration user's, by login; secrets set."""
    logins = {"shop_admin": FakeServer(storage="local_directory"), "shop_migrator": FakeServer()}
    opened = []

    def open_client(settings, login):
        opened.append(login.user)
        return logins[login.user]

    monkeypatch.setattr("ch_migrate.access.bootstrap.open_client", open_client)
    for name, value in PASSWORDS.items():
        monkeypatch.setenv(f"CH_DEV_{name}_PASSWORD", value)
    admin = logins["shop_admin"]
    admin.opened = opened
    return admin, logins["shop_migrator"]


@pytest.fixture
def offline(monkeypatch):
    """No CH_* variables, and any socket use fails the test."""
    for name in [name for name in os.environ if name.startswith("CH_")]:
        monkeypatch.delenv(name)

    def refuse(*args, **kwargs):
        raise AssertionError("bootstrap --dry-run opened a socket")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


def declaration(project):
    return validate_access(load_settings("dev", project / "config.yaml"), project).spec


def set_config(project, line):
    """Add one line under the `dev` environment in config.yaml."""
    path = project / "config.yaml"
    path.write_text(
        path.read_text().replace("    host: localhost\n", f"    host: localhost\n{line}\n", 1)
    )


def sent(server):
    return [write.sql for write in server.writes]


@pytest.mark.parametrize("access_yaml", [None, DECLARED])
def test_dry_run_needs_no_secret_and_no_network(project, offline, access_yaml):
    if access_yaml:
        (project / "access.yaml").write_text(access_yaml)
    result = bootstrap("dev", "--dry-run")
    assert result.exit_code == 0, result.output
    statements = [line for line in result.stdout.splitlines() if not line.startswith("--")]
    assert statements[:4] == [
        "CREATE DATABASE IF NOT EXISTS shop_dev;",
        "CREATE ROLE IF NOT EXISTS shop_migration_role;",
        "CREATE USER IF NOT EXISTS shop_migrator IDENTIFIED BY <secret:migration_password>;",
        "GRANT shop_migration_role TO shop_migrator;",
    ]
    # With access.yaml, bootstrap makes the migration identity only: `apply` makes the rest.
    assert "analyst" not in result.stdout
    assert ("WITH GRANT OPTION" in result.stdout) == (access_yaml is None)


@pytest.mark.parametrize("password_set", [False, True])
def test_dry_run_leaves_out_an_optional_user_with_no_password_set(
    project, offline, monkeypatch, password_set
):
    """--dry-run shows what the real run makes, and still reads no secret:
    it asks only whether CH_<ENV>_MCP_PASSWORD or an SSM path is set."""
    set_config(project, "    mcp_user_name: shop_mcp")
    if password_set:
        monkeypatch.setenv("CH_DEV_MCP_PASSWORD", "never-read")
    result = bootstrap("dev", "--dry-run")
    assert result.exit_code == 0, result.output
    assert ("shop_mcp" in result.stdout) is password_set
    assert "never-read" not in result.output
    assert ("skips that user" in result.stderr) is not password_set


def test_dry_run_says_where_on_cluster_would_apply(project, offline):
    (project / "access.yaml").write_text(CORE)
    set_config(project, "    cluster: shop_cluster")
    result = bootstrap("dev", "--dry-run")
    assert result.exit_code == 0, result.output
    assert "-- With cluster shop_cluster set, bootstrap runs each statement ON CLUSTER" in (
        result.stdout
    )
    statements = [line for line in result.stdout.splitlines() if not line.startswith("--")]
    for form in (" ON CLUSTER shop_cluster", " ON CLUSTER `shop_cluster`"):
        assert statements and not any(form in sql for sql in statements), statements


def test_legacy_sends_every_statement_roles_and_users_first(project, servers):
    """No access.yaml: 0.8's statements, on every run, never ON CLUSTER (Decision 13)."""
    admin, migration = servers
    set_config(project, "    cluster: shop_cluster")
    admin.converges_to = declaration(project)
    for _ in range(2):
        result = bootstrap("dev")
        assert result.exit_code == 0, result.output
    statements = sent(admin)
    assert len(statements) == 2 * 27  # 0.8's 27 statements, the database's included
    assert statements[:4] == [
        "CREATE DATABASE IF NOT EXISTS `shop_dev`",
        "CREATE ROLE IF NOT EXISTS `shop_migration_role`",
        "CREATE USER IF NOT EXISTS `shop_migrator` IDENTIFIED BY {password:String}",
        "GRANT `shop_migration_role` TO `shop_migrator`",
    ]
    assert not any("ON CLUSTER" in sql for sql in statements)
    assert "GRANT CURRENT GRANTS(READ ON REMOTE) TO `shop_migration_role`" in statements
    assert admin.opened == ["shop_admin", "shop_migrator"] * 2
    assert result.stdout.splitlines()[-3:-1] == [
        "✓ user shop_migrator holds every required grant of role shop_migration_role (15).",
        "✓ Bootstrap complete for dev.",
    ]


@pytest.mark.parametrize(
    ("key", "secret", "env"),
    [
        ("mcp_user_name", "mcp_password", "CH_DEV_MCP_PASSWORD"),
        ("dict_reader_name", "dict_reader_password", "CH_DEV_DICT_READER_PASSWORD"),
    ],
)
def test_legacy_skips_an_optional_user_whose_password_isnt_set(
    project, servers, monkeypatch, key, secret, env
):
    """0.8 made an optional user only when its password was set."""
    admin, migration = servers
    monkeypatch.delenv(env, raising=False)
    set_config(project, f"    {key}: shop_extra")
    admin.converges_to = declaration(project)
    result = bootstrap("dev")
    assert result.exit_code == 0, result.output
    assert not any("shop_extra" in sql for sql in sent(admin)), sent(admin)
    assert (
        f"! {key} names shop_extra, but {secret} isn't set, so `bootstrap` skips that user "
        "and its role, as 0.8 did."
    ) in result.stderr.splitlines()


def test_legacy_attempts_every_grant_and_names_each_failure(project, servers):
    """Decision 6: one refused GRANT doesn't stop the rest; a refused best-effort one warns."""
    admin, migration = servers
    admin.failing = {
        "CREATE FUNCTION": "Code: 497. Not enough privileges. (ACCESS_DENIED)",
        "CURRENT GRANTS(READ ON REMOTE)": "Code: 62. Syntax error. (SYNTAX_ERROR)",
    }
    migration.lacking = {"CREATE FUNCTION"}
    result = bootstrap("dev")
    assert result.exit_code == 1, result.output
    assert len(sent(admin)) == 27
    errors = [line for line in result.stderr.splitlines() if line.startswith("✗")]
    assert errors == [
        "✗ grant CREATE FUNCTION, DROP FUNCTION ON *.* WITH GRANT OPTION to role "
        "shop_migration_role: Code: 497. Not enough privileges. (ACCESS_DENIED)",
        "✗ user shop_migrator lacks CREATE FUNCTION, DROP FUNCTION ON *.*",
        "✗ user shop_migrator lacks 1 of the 15 required grants of role shop_migration_role.",
        "✗ Bootstrap of dev isn't complete: 1 change failed; "
        "user shop_migrator lacks 1 required grant.",
    ]
    assert (
        "! grant role shop_migration_role as much of READ ON REMOTE as the admin holds "
        "(best-effort): Code: 62. Syntax error. (SYNTAX_ERROR)"
    ) in result.stderr


@pytest.mark.parametrize("access_yaml", [CORE, None], ids=["access.yaml", "legacy"])
def test_a_server_older_than_25_8_exits_2_before_any_write(project, servers, access_yaml):
    """Every bootstrap needs 25.8, legacy too: 0.8's failed on 24.8."""
    admin, migration = servers
    if access_yaml is not None:
        (project / "access.yaml").write_text(access_yaml)
    admin.server_version = "24.8.14.39"
    result = bootstrap("dev")
    assert result.exit_code == 2, result.output
    assert "The server runs ClickHouse 24.8.14.39." in result.stderr
    assert admin.writes == []


def test_with_access_yaml_only_the_migration_identity_is_made(project, servers):
    admin, migration = servers
    (project / "access.yaml").write_text(DECLARED)
    spec = declaration(project)
    admin.converges_to = spec.__class__(spec.environment, spec.migration)
    result = bootstrap("dev")
    assert result.exit_code == 0, result.output
    assert sent(admin)[:3] == [
        "CREATE DATABASE IF NOT EXISTS `shop_dev`",
        "CREATE ROLE IF NOT EXISTS `shop_migration_role`",
        "CREATE USER IF NOT EXISTS `shop_migrator` IDENTIFIED BY {password:String}",
    ]
    assert not any("analyst" in sql or "WITH GRANT OPTION" in sql for sql in sent(admin))
    assert result.stdout.splitlines()[:2] == [
        "→ Bootstrapping dev: database shop_dev on localhost, as shop_admin, "
        "with the core profile",
        "+ create database shop_dev if it doesn't exist",
    ]
    # Run again: in sync, so only the database's IF NOT EXISTS goes out.
    admin.writes.clear()
    assert bootstrap("dev").exit_code == 0
    assert sent(admin) == ["CREATE DATABASE IF NOT EXISTS `shop_dev`"]


def test_changes_still_pending_after_the_writes_are_named_and_exit_1(project, servers):
    """A replica still behind at the deadline leaves the migration
    identity pending, though the check on the node connected to passes."""
    admin, migration = servers
    (project / "access.yaml").write_text(DECLARED)
    set_config(project, "    access_read_timeout: 0")
    result = bootstrap("dev")  # the admin's server never shows what was written
    assert result.exit_code == 1, result.output
    assert "→ Still pending for dev:" in result.stdout.splitlines()
    assert "+ create role shop_migration_role" in result.stdout.splitlines()
    assert result.stderr.splitlines()[-1].startswith("✗ Bootstrap of dev isn't complete: ")
    assert "still pending" in result.stderr.splitlines()[-1]


@pytest.mark.parametrize(
    ("failing", "closing"),
    [
        ({}, "planning again found 2 errors"),
        ({"CREATE ROLE": DENIED}, "1 change failed; planning again found 2 errors"),
    ],
    ids=["plan errors only", "a failed change too"],
)
def test_the_closing_line_counts_failed_changes_apart_from_plan_errors(
    project, servers, monkeypatch, failing, closing
):
    """An error in the plan read back after the writes isn't a failed change, but it still
    leaves the bootstrap incomplete, with exit 1."""
    admin, migration = servers
    (project / "access.yaml").write_text(CORE)
    admin.failing = failing
    errors = Plan(errors=("first error", "second error"))
    monkeypatch.setattr("ch_migrate.access.applier.replan", lambda work, outcome: errors)
    result = bootstrap("dev")
    assert result.exit_code == 1, result.output
    assert result.stderr.splitlines()[-1] == f"✗ Bootstrap of dev isn't complete: {closing}."


def test_with_access_yaml_a_migration_user_that_is_the_admin_exits_2(project, servers):
    admin, migration = servers
    (project / "access.yaml").write_text(CORE)
    config = project / "config.yaml"
    config.write_text(
        config.read_text().replace("admin_user: shop_admin", "admin_user: shop_migrator", 1)
    )
    migration.opened = []
    result = bootstrap("dev")
    assert result.exit_code == 2, result.output
    assert "is config.yaml's admin_user" in result.stderr
    assert migration.writes == [] and admin.writes == []


@pytest.mark.parametrize("access_yaml", [None, CORE])
def test_a_missing_secret_exits_2_before_any_write(project, servers, monkeypatch, access_yaml):
    admin, migration = servers
    if access_yaml:
        (project / "access.yaml").write_text(access_yaml)
    monkeypatch.delenv("CH_DEV_MIGRATION_PASSWORD")
    result = bootstrap("dev")
    assert result.exit_code == 2, result.output
    assert "CH_DEV_MIGRATION_PASSWORD" in result.stderr
    assert admin.writes == []


def test_an_invalid_access_yaml_exits_2_listing_each_error(project, servers):
    admin, migration = servers
    (project / "access.yaml").write_text("migration_role: {profile: nope}\nusers: [x]\n")
    result = bootstrap("dev")
    assert result.exit_code == 2, result.output
    assert result.stderr.splitlines()[-1] == "✗ Invalid: 2 errors."
    assert admin.opened == []


def test_an_empty_access_yaml_exits_2_instead_of_giving_core(project, servers):
    """An empty file isn't legacy mode, and isn't core either."""
    admin, migration = servers
    (project / "access.yaml").write_text("")
    result = bootstrap("dev")
    assert result.exit_code == 2, result.output
    assert "access.yaml is empty" in result.stderr
    assert admin.opened == []


def test_ctrl_c_during_a_write_exits_130(project, servers):
    """The code `run` returns reaches the command line, as `up`'s does."""
    admin, migration = servers

    def interrupt(sql, parameters=None, settings=None):
        answer = FakeServer.command(admin, sql, parameters, settings)
        if admin.writes:  # a CHECK GRANT while reading isn't a write
            raise KeyboardInterrupt
        return answer

    admin.command = interrupt
    result = bootstrap("dev")
    assert result.exit_code == 130, result.output
    assert "`bootstrap` stopped part-way: interrupted." in result.stderr


def test_verbose_prints_each_statement_and_never_a_password(project, servers):
    admin, migration = servers
    admin.converges_to = declaration(project)
    result = bootstrap("dev", "--verbose")
    assert result.exit_code == 0, result.output
    assert "  CREATE ROLE IF NOT EXISTS `shop_migration_role`;" in result.stdout
    assert not any(password in result.output for password in PASSWORDS.values())
