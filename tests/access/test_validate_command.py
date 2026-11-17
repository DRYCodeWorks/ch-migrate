"""`ch-migrate access validate ENV [--sql]`: offline, exit 0 when valid and 2 when not."""

import os
import socket

import pytest
from click.testing import CliRunner

from ch_migrate.cli import main

VALID = """\
roles:
  analyst: {grants: ['SELECT ON {db}.*']}
users:
  dashboards: {password: {secret: dashboards_password}, roles: [analyst]}
"""
INVALID = """\
roles:
  analyst: {grant: ['SELECT ON {db}.*']}
  loader: {grants: [REMOTE ON *.*, 'SELECT ON {db}.t*']}
users:
  etl: {password: hunter2}
"""


def run(*args):
    return CliRunner().invoke(main, ["access", "validate", *args])


@pytest.fixture
def offline(monkeypatch):
    """No CH_* variables, and any socket use fails the test."""
    for name in [name for name in os.environ if name.startswith("CH_")]:
        monkeypatch.delenv(name)

    def refuse(*args, **kwargs):
        raise AssertionError("access validate opened a socket")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


def test_a_valid_file_exits_0(project):
    (project / "access.yaml").write_text(VALID)
    result = run("dev")
    assert result.exit_code == 0, result.output
    assert result.stdout == (
        "✓ access.yaml is valid for dev: 1 role and 1 user declared; "
        "the migration role shop_migration_role holds the core profile.\n"
    )


def test_an_invalid_file_exits_2_listing_every_error(project):
    (project / "access.yaml").write_text(INVALID)
    result = run("dev")
    assert result.exit_code == 2, result.output
    assert result.stdout == ""
    assert result.stderr.splitlines() == [
        "✗ access.yaml: roles.analyst.grant: unknown key; the keys here are grants, roles or absent",
        "✗ access.yaml: roles.loader.grants[0]: `REMOTE ON *.*`: this is the old spelling of a "
        "source grant: write `READ, WRITE ON REMOTE`",
        "✗ access.yaml: roles.loader.grants[1]: `SELECT ON {db}.t*`: prefix targets such as "
        "`shop_dev.t*` aren't supported in v1",
        "✗ access.yaml: users.etl.password: expected {secret: NAME}; "
        "access.yaml never holds a password",
        "✗ Invalid: 4 errors.",
    ]


def test_warnings_leave_the_exit_code_at_0(project):
    (project / "access.yaml").write_text("roles: {a: {grants: ['SELEKT ON {db}.*']}}\n")
    result = run("dev")
    assert result.exit_code == 0, result.output
    assert result.stderr.startswith("! access.yaml: roles.a.grants[0]: `SELEKT ON {db}.*`: ")


def test_legacy_mode_without_access_yaml(project):
    result = run("dev")
    assert result.exit_code == 0, result.output
    assert result.stdout == (
        "✓ No access.yaml: legacy mode, where the migration role shop_migration_role "
        "holds the legacy profile.\n"
    )


@pytest.mark.parametrize("access_yaml", [VALID, None])
def test_runs_with_no_ch_variables_and_sockets_blocked(project, offline, access_yaml):
    config = (project / "config.yaml").read_text()
    ssm = "    ssm: {migration_password: /shop/m, dashboards_password: /shop/d}\n"
    extra = "    mcp_user_name: shop_mcp\n    dict_reader_name: shop_dict\n" + ssm
    (project / "config.yaml").write_text(config.replace("    admin_user: shop_admin\n", extra, 1))
    if access_yaml:
        (project / "access.yaml").write_text(access_yaml)
    for args in (("dev",), ("dev", "--sql")):
        result = run(*args)
        assert result.exit_code == 0, result.output


def test_sql_goes_to_stdout_and_the_summary_to_stderr(project):
    (project / "access.yaml").write_text(VALID)
    result = run("dev", "--sql")
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[:4] == [
        "-- Access for dev, from access.yaml.",
        "-- Replace each <secret:NAME> with that secret's value as a quoted string, such as",
        "-- 'value', before running it. Left in place, it fails to parse.",
        "CREATE ROLE IF NOT EXISTS shop_migration_role;",
    ]
    assert (
        "CREATE USER IF NOT EXISTS dashboards IDENTIFIED BY <secret:dashboards_password>;" in lines
    )
    assert lines[-1] == "GRANT SELECT ON shop_dev.* TO analyst;"
    assert result.stderr.startswith("✓ access.yaml is valid for dev")


def test_sql_names_users_with_no_password_and_keeps_their_roles_and_grants(project):
    (project / "access.yaml").write_text(
        VALID + "  viewer: {roles: [analyst], default_roles: [analyst]}\n  ops: {}\n"
    )
    result = run("dev", "--sql")
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[3] == (
        "-- Users with no password aren't created here and must already exist: viewer, ops."
    )
    creates = [line for line in lines if line.startswith("CREATE USER")]
    assert creates == [
        "CREATE USER IF NOT EXISTS shop_migrator IDENTIFIED BY <secret:migration_password>;",
        "CREATE USER IF NOT EXISTS dashboards IDENTIFIED BY <secret:dashboards_password>;",
    ]
    assert "GRANT analyst TO viewer;" in lines
    assert "SET DEFAULT ROLE analyst TO viewer;" in lines
    assert "users.viewer: no password, so it is never created" in result.stderr


def test_an_invalid_file_prints_no_sql(project):
    (project / "access.yaml").write_text(INVALID)
    result = run("dev", "--sql")
    assert result.exit_code == 2
    assert result.stdout == ""


@pytest.mark.parametrize(
    "setup, error",
    [
        (lambda project: None, "✗ Unknown environment: staging. Available: dev, production\n"),
        (
            lambda project: (project / "config.yaml").unlink(),
            "✗ Configuration file not found: {project}/config.yaml\n",
        ),
        (
            lambda project: (project / "config.yaml").write_text("environments:\n  staging: [\n"),
            "✗ config.yaml: line 3, column 1: not valid YAML: "
            "expected the node content, but found '<stream end>'\n",
        ),
        (
            lambda project: (project / "config.yaml").write_text("environments: {staging: x}\n"),
            "✗ environments.staging in config.yaml must be a mapping\n",
        ),
    ],
)
def test_config_errors_exit_2(project, setup, error):
    setup(project)
    result = run("staging")
    assert result.exit_code == 2
    assert result.stderr == error.format(project=project)


@pytest.mark.parametrize("command", [["show", "dev"], ["plan", "dev"], ["apply", "dev"]])
def test_an_empty_access_yaml_stops_a_live_command_before_it_connects(
    project, monkeypatch, command
):
    """`access show dev > access.yaml` empties the file first."""
    (project / "access.yaml").write_text("")

    def refuse(settings):
        raise AssertionError("connected")

    monkeypatch.setattr("ch_migrate.access.cli.connect_admin", refuse)
    result = CliRunner().invoke(main, ["access", *command])
    assert result.exit_code == 2, result.output
    assert "access.yaml is empty" in result.stderr
