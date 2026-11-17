"""Access-test fixtures: projects whose admin and migration user are different logins.

`conftest.py` imports the fixtures so every integration test can request them.

Each project gets one random name, `it_<16 hex>`. Its database is `<name>_db`, and every user
and role a fixture creates starts with the name. A test that creates its own users or roles
names them the same way, and the finalizer drops every user and role with that prefix.
`read_access` turns those generated names back into `{project}` and `{db}`.
"""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner, Result

from ch_migrate.cli import main

# The 2026-10-29 limited admin, granted by the server's admin. T2 refines it. Its rights on
# the database and on `system.*` are as much of them as the server's admin holds: all, on a
# stock server; less on ClickHouse Cloud, whose admin holds neither `system.zookeeper` nor
# every privilege on a database.
LIMITED_ADMIN_GRANTS = (
    "GRANT CURRENT GRANTS(ALL ON {db}.*) TO {user} WITH GRANT OPTION",
    "GRANT CREATE ROLE, CREATE USER, ROLE ADMIN ON *.* TO {user}",
    "GRANT CURRENT GRANTS(SELECT ON system.*) TO {user} WITH GRANT OPTION",
)
# On 25.8 and later, the `limited_admin` fixture also gets READ ON REMOTE, without grant
# option, to read every replica where users and roles are replicated (design note, 2.5).
# 24.8 can't parse it.
REPLICA_READ_GRANT = "GRANT READ ON REMOTE TO {user}"

# By server: whether ON CLUSTER needs the CLUSTER grant there (`requires_cluster_grant`).
_REQUIRES_CLUSTER_GRANT: dict[tuple[str, int], bool] = {}

# Generated names: a database is the project name plus `_db`.
_GENERATED_NAME = re.compile(r"\bit_[0-9a-f]{16}(_db\b)?")

_ACCESS_QUERIES = {
    "grants": (
        "SELECT * FROM system.grants "
        "WHERE user_name IN {names:Array(String)} OR role_name IN {names:Array(String)}"
    ),
    "role_grants": (
        "SELECT * EXCEPT granted_role_id FROM system.role_grants "
        "WHERE user_name IN {names:Array(String)} OR role_name IN {names:Array(String)}"
    ),
    "users": (
        "SELECT name, default_roles_all, default_roles_list, default_roles_except "
        "FROM system.users WHERE name IN {names:Array(String)}"
    ),
    "roles": "SELECT name FROM system.roles WHERE name IN {names:Array(String)}",
}


@dataclass(frozen=True)
class AccessProject:
    """A project for environment `it` whose migration user is not its admin."""

    root: Path
    client: Any = field(repr=False)  # the server admin's connection, whoever the project's admin is
    name: str

    @property
    def database(self) -> str:
        return self.name + "_db"

    @property
    def migration_user(self) -> str:
        return self.name + "_migration"

    @property
    def admin(self) -> str:
        """config.yaml's admin_user: the server's admin, unless a test made another."""
        config = yaml.safe_load((self.root / "config.yaml").read_text())
        return config["environments"]["it"]["admin_user"]

    @property
    def migration_role(self) -> str:
        """The role 0.8's `bootstrap` creates, named after the project."""
        return self.name + "_migration_role"

    @staticmethod
    def new_password() -> str:
        # ClickHouse Cloud's password policy needs an uppercase letter and a special character.
        return "Chm!" + secrets.token_hex(24)

    def run(self, *args: str) -> Result:
        return CliRunner().invoke(main, list(args), catch_exceptions=True)

    def configure(self, **settings: Any) -> None:
        """Set keys under `environments.it` in config.yaml."""
        path = self.root / "config.yaml"
        config = yaml.safe_load(path.read_text())
        config["environments"]["it"].update(settings)
        path.write_text(yaml.safe_dump(config))


@pytest.fixture
def access_project(clickhouse_server, request):
    """A project that is not bootstrapped yet; the server's admin is its admin.

    It has no access.yaml, so it starts in legacy mode, as a 0.8 project does. A test that
    wants an access.yaml writes its own.
    """
    return new_access_project(clickhouse_server, request)


def new_access_project(server, request, root=None):
    """`access_project` on `server`, in `root` or the test's tmp_path, as the working directory."""
    root = root or request.getfixturevalue("tmp_path")
    monkeypatch = request.getfixturevalue("monkeypatch")
    name = "it_" + secrets.token_hex(8)
    result = CliRunner().invoke(main, ["init", str(root), "--name", name])
    assert result.exit_code == 0, result.output
    (root / "access.yaml").unlink()  # `init` writes one for a new project
    client = server.connect()
    request.addfinalizer(client.close)
    project = AccessProject(root, client, name)
    request.addfinalizer(lambda: _drop_project(project))
    _write_config(project, server)
    monkeypatch.setenv("CH_IT_ADMIN_PASSWORD", server.password)
    monkeypatch.setenv("CH_IT_MIGRATION_PASSWORD", AccessProject.new_password())
    monkeypatch.chdir(root)
    return project


def initialize_cluster_project(cluster, project, monkeypatch):
    """Initialize a project on node 1; the caller owns its cluster-wide cleanup."""
    result = project.run("init", str(project.root), "--name", project.name)
    assert result.exit_code == 0, result.output
    server = cluster.servers[1]
    _write_config(project, server)
    project.configure(cluster=cluster.name)
    monkeypatch.setenv("CH_IT_ADMIN_PASSWORD", server.password)
    monkeypatch.chdir(project.root)


@pytest.fixture
def bootstrapped_project(access_project):
    """`access_project` after `ch-migrate bootstrap` created its database, role and user."""
    result = access_project.run("bootstrap", "it")
    assert result.exit_code == 0, result.output
    return access_project


@pytest.fixture
def limited_admin(access_project, clickhouse_server, monkeypatch):
    """Make the project's admin a login with only `LIMITED_ADMIN_GRANTS`; return that login."""
    login = replace(
        clickhouse_server,
        user=access_project.name + "_admin",
        password=AccessProject.new_password(),
    )
    client = access_project.client
    client.command(f"CREATE USER {login.user} IDENTIFIED BY '{login.password}'")
    for grant in (*LIMITED_ADMIN_GRANTS, REPLICA_READ_GRANT):
        client.command(grant.format(db=access_project.database, user=login.user))
    access_project.configure(admin_user=login.user)
    monkeypatch.setenv("CH_IT_ADMIN_PASSWORD", login.password)
    return login


def requires_cluster_grant(server):
    """Whether ON CLUSTER needs the CLUSTER grant here, as a stock server's does.

    Asked as a new login with no grant at all: ClickHouse Cloud, for one, grants CLUSTER to
    every login, so there `CHECK GRANT CLUSTER ON *.*` answers 1 for it.
    """
    key = (server.host, server.port)
    if key not in _REQUIRES_CLUSTER_GRANT:
        _REQUIRES_CLUSTER_GRANT[key] = _probe_cluster_grant(server)
    return _REQUIRES_CLUSTER_GRANT[key]


def query_log(client):
    """Flush the query log, and return a FROM source that reads it on every replica.

    ClickHouse Cloud logs each query on the replica that served it. So where the `default`
    cluster lists more than one host, every replica's log is flushed and read, rotated
    `query_log_N` tables included.
    """
    if client.command("SELECT count() FROM system.clusters WHERE cluster = 'default'") > 1:
        client.command("SYSTEM FLUSH LOGS ON CLUSTER 'default'")
        return "clusterAllReplicas('default', merge('system', '^query_log'))"
    client.command("SYSTEM FLUSH LOGS")
    return "system.query_log"


def read_access(client, users, roles):
    """What the server holds for these users and roles: sorted rows, generated names replaced.

    `grants` and `role_grants` hold the rows granted to them, `users` their default roles,
    and `roles` the roles that exist.
    """
    names = [*users, *roles]
    return {key: _access_rows(client, query, names) for key, query in _ACCESS_QUERIES.items()}


def _write_config(project, server):
    environment = {
        "host": server.host,
        "port": server.port,
        "secure": server.secure,
        "database": project.database,
        "admin_user": server.user,
        "migration_user": project.migration_user,
    }
    config = {"project": {"name": project.name}, "environments": {"it": environment}}
    (project.root / "config.yaml").write_text(yaml.safe_dump(config))


def _drop_project(project):
    """Drop the database and every user and role whose name starts with the project's."""
    client = project.client
    client.command(f"DROP DATABASE IF EXISTS {project.database} SYNC")
    for kind, table in (("USER", "system.users"), ("ROLE", "system.roles")):
        names = client.query(
            f"SELECT name FROM {table} WHERE startsWith(name, {{prefix:String}})",
            parameters={"prefix": project.name},
        ).result_rows
        for (name,) in names:
            client.command(f"DROP {kind} IF EXISTS {name}")


def _access_rows(client, query, names):
    result = client.query(query, parameters={"names": names})
    rows = [
        {column: _placeholders(value) for column, value in zip(result.column_names, row)}
        for row in result.result_rows
    ]
    return sorted(rows, key=lambda row: json.dumps(row, sort_keys=True))


def _placeholders(value):
    if isinstance(value, str):
        return _GENERATED_NAME.sub(lambda m: "{db}" if m.group(1) else "{project}", value)
    if isinstance(value, (list, tuple)):
        return [_placeholders(item) for item in value]
    return value


def _probe_cluster_grant(server):
    admin = server.connect()
    login = replace(
        server, user="it_" + secrets.token_hex(8) + "_probe", password=AccessProject.new_password()
    )
    try:
        admin.command(
            f"CREATE USER {login.user} IDENTIFIED BY {{password:String}}",
            parameters={"password": login.password},
        )
        client = login.connect()
        try:
            return client.command("CHECK GRANT CLUSTER ON *.*") == 0
        finally:
            client.close()
    finally:
        admin.command(f"DROP USER IF EXISTS {login.user}")
        admin.close()
