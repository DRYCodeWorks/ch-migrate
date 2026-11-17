"""A stand-in for the admin's connection: canned rows for each statement in `sql/live.sql`.

`FakeServer.hold(spec)` fills its tables with what a declaration asks for, as if the SQL
from `access validate --sql` had been run. `empty_reads` makes the first reads find no user,
role, grant or membership, as a replica that is behind would.

For `access apply` it answers `currentUser()` with `login` and records every write in
`writes`. A write whose SQL contains a key of `failing` raises that message, and after any
other write the server holds `converges_to`, when that is set.

For `access check` it records each `CHECK GRANT` in `checks`, and answers 0 for one that
contains an entry of `lacking`, else 1.

It answers `hostName()` with `node`, or refuses it when `node` is None. That isn't a read of
access, so it isn't recorded in `reads`.

Where users and roles are replicated, the reader reads every replica at once (design note,
2.5). `clusters` are the clusters that hold this node, each with its hosts, as
`system.clusters` lists them, and `replicas` are the replicas each read reaches. Each
replica holds the same tables, except that one named in `behind` shows nothing for that
many reads. With `replica_error` set, every read of every replica fails with that message.
A read of a table on every replica is recorded in `reads` under the table's own name, as one
read, and its parameters and settings in `replica_parameters` and `replica_settings`.

The admin's own grants are `own_grants`, `ALL ON *.* WITH GRANT OPTION` unless a test sets
them. With none, its `CHECK GRANT CLUSTER` still answering 1 is a server that grants CLUSTER
to every login, as ClickHouse Cloud does.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from clickhouse_connect.driver.exceptions import DatabaseError

from ch_migrate.access.grants import parse_grant
from ch_migrate.access.privileges import BUNDLED_LIST, bundled_tree
from ch_migrate.access.templates import statement

STATEMENTS = (
    "privileges",
    "storage",
    "roles",
    "users",
    "grants",
    "role_grants",
    "accepted_sources",
    "own_grants",
    "clusters",
    "replicas",
    "replica_roles",
    "replica_users",
    "replica_grants",
    "replica_role_grants",
)
ENTITY_TABLES = ("roles", "users", "grants", "role_grants")


@dataclass(frozen=True)
class Written:
    """One statement `apply` sent: its SQL, bound parameters and settings."""

    sql: str
    parameters: dict
    settings: dict


class FakeServer:
    """Answers the reader's queries from tables of rows, as the system tables would."""

    server_version = "26.3.1.1"

    def __init__(self, storage="replicated"):
        self.storage = storage  # None: the login can't read system.user_directories
        self.tables = {name: [] for name in ENTITY_TABLES}
        self.sources = {"REMOTE", "S3"}
        self.empty_reads = 0
        self.reads = []
        self.closed = False
        self.login = "shop_admin"
        self.writes = []
        self.raw = []  # the statements sent by raw_query
        self.login_roles = []  # the roles the login apply runs as holds
        self.failing = {}
        self.converges_to = None
        self.checks = []
        self.lacking = set()
        self.node = "node-1"
        self.clusters = [{"cluster": "default", "hosts": ["node-1:9000", "node-2:9000"]}]
        self.replicas = ("node-1", "node-2")
        self.behind = {}
        self.replica_error = None
        self.replica_settings = []
        self.replica_parameters = []
        self.own_grants = grant_rows(parse_grant("ALL ON *.* WITH GRANT OPTION"))

    def query(self, sql, parameters=None, settings=None):
        if sql == statement("apply", "current_user"):
            return _Result([{"name": self.login}])
        if sql == statement("apply", "current_roles"):
            return _Result([{"name": role} for role in self.login_roles])
        if sql == statement("live", "node"):
            if self.node is None:
                raise DatabaseError("Code: 497. Not enough privileges. (ACCESS_DENIED)")
            return _Result([{"name": self.node}])
        [name] = [name for name in STATEMENTS if statement("live", name) == sql]
        if name.startswith("replica"):
            return _Result(self._every_replica(name, parameters or {}, settings))
        self.reads.append(name)
        return _Result(self._rows(name, parameters or {}))

    def raw_query(self, sql, parameters=None, settings=None):
        """`apply` sends a statement with bound parameters, a password, this way."""
        self.raw.append(sql)
        return self.command(sql, parameters, settings)

    def command(self, sql, parameters=None, settings=None):
        if sql.startswith("CHECK GRANT"):
            self.checks.append(sql)
            return 0 if any(part in sql for part in self.lacking) else 1
        self.writes.append(Written(sql, parameters or {}, settings or {}))
        for part, message in self.failing.items():
            if part in sql:
                raise DatabaseError(message)
        if self.converges_to is not None:
            self.tables = {name: [] for name in ENTITY_TABLES}
            self.hold(self.converges_to)

    def close(self):
        self.closed = True

    def hold(self, spec):
        """Hold everything `spec` declares: its roles, users, memberships and grants."""
        migration = spec.migration
        self._add(("role", migration.role), migration.all_grants)
        self._add(("user", migration.user), roles=(migration.role,))
        for role in spec.roles:
            self._add(("role", role.name), role.grants, role.roles)
        for user in spec.users:
            self._add(("user", user.name), user.grants, user.roles)

    def _add(self, entity, grants=(), roles=()):
        kind, name = entity
        self.tables[f"{kind}s"].append(
            {"name": name, "default_roles_all": 1, "default_roles_except": []}
        )
        holder = {"user_name": None, "role_name": None, f"{kind}_name": name}
        for role in roles:
            self.tables["role_grants"].append(
                {
                    **holder,
                    "granted_role_name": role,
                    "granted_role_is_default": 1,
                    "with_admin_option": 0,
                }
            )
        for grant in grants:
            self.tables["grants"] += [{**holder, **row} for row in grant_rows(grant)]

    def _every_replica(self, name, parameters, settings):
        self.replica_settings.append(settings)
        self.replica_parameters.append(parameters)
        if self.replica_error is not None:
            raise DatabaseError(self.replica_error)
        if name == "replicas":
            return [{"replica": replica} for replica in self.replicas]
        table = name.removeprefix("replica_")
        self.reads.append(table)
        rows = self._rows(table, parameters)
        reads = self.reads.count("privileges")
        return [
            {"replica": replica, **row}
            for replica in self.replicas
            if reads > self.behind.get(replica, 0)
            for row in rows
        ]

    def _rows(self, name, parameters):
        if name == "own_grants":
            return self.own_grants
        if name == "clusters":
            return self.clusters
        if name == "privileges":
            return json.loads(BUNDLED_LIST.read_text())["privileges"]
        if name == "storage":
            if self.storage is None:
                raise DatabaseError("Code: 497. Not enough privileges. (ACCESS_DENIED)")
            return [{"type": self.storage}]
        if name == "accepted_sources":
            return [{"source": s} for s in parameters["sources"] if s in self.sources]
        if self.reads.count("privileges") <= self.empty_reads:
            return []  # each read starts with the privilege tree
        names = parameters["names"]
        return [
            row
            for row in self.tables[name]
            if {row.get("name"), row.get("user_name"), row.get("role_name")} & set(names)
        ]


def grant_rows(grant, **flags):
    """`system.grants` rows for one grant: one per privilege, under its canonical name."""
    tree, target = bundled_tree(), grant.target
    return [
        {
            "access_type": tree.canonical(privilege) or privilege,
            "access_object": target.source or "",
            "database": target.database,
            "table": target.table,
            "column": None,
            "is_partial_revoke": 0,
            "grant_option": int(grant.grant_option),
            **flags,
        }
        for privilege in grant.privileges
    ]


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def named_results(self):
        return iter(self._rows)
