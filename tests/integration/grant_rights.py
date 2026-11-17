"""Leaf expansion: the read-only grant comparison that the access spike tests (U1).

ClickHouse folds what it stores in `system.grants`. A group may be stored as itself or as
its children, a narrower grant is absorbed by a broader one, and an alias is stored under
its canonical name. So the spike compares *rights* instead: one per privilege flag, scope
and grant option, expanded from `system.privileges` on the server being compared.

A privilege with a `level` carries a flag of its own; one without is a group. A privilege
means its own flag plus its descendants' flags, keeping only the flags whose level the
target's scope allows. `CREATE ON db.*` therefore means `CREATE DATABASE`, `CREATE TABLE`,
`CREATE VIEW` and `CREATE DICTIONARY`, which is also what the server stores for it.

This is test code. The spike record, `docs/design/2026-11-11-access-spike.md`, describes
the algorithm for the product code that replaces it.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

# Levels a flag may have and still be granted on a database target, or on a table target.
_DATABASE_LEVELS = frozenset({"DATABASE", "TABLE", "DICTIONARY", "VIEW", "COLUMN"})
_TABLE_LEVELS = frozenset({"TABLE", "DICTIONARY", "VIEW", "COLUMN"})

# `[GRANT] [CURRENT GRANTS(]<privileges> ON <target>[)] [TO <grantee>] [WITH GRANT OPTION]`
_GRANT = re.compile(
    r"^(?:GRANT\s+)?(?P<current>CURRENT\s+GRANTS\s*\()?\s*(?P<privileges>.+?)\s+ON\s+"
    r"(?P<target>[\w*]+(?:\.[\w*]+)?)\s*\)?(?:\s+TO\s+\w+)?(?P<option>\s+WITH\s+GRANT\s+OPTION)?"
    r"\s*;?$",
    re.IGNORECASE | re.DOTALL,
)

_PRIVILEGES = "SELECT privilege, aliases, level, parent_group FROM system.privileges"


@dataclass(frozen=True, order=True)
class Scope:
    """Where a right applies: `*.*`, `db.*`, `db.table`, a column, or a source like `REMOTE`."""

    database: str | None = None
    table: str | None = None
    source: str = ""
    column: str | None = None

    @property
    def kind(self) -> str:
        if self.source:
            return "source"
        if self.database is None:
            return "global"
        return "database" if self.table is None else "table"

    def contains(self, other: Scope) -> bool:
        """True when everything `other` covers is inside this scope."""
        if self.kind == "global":
            return True
        if self.source or other.source:
            return self.source == other.source and other.database is None
        if self.database != other.database:
            return False
        if self.table is None:
            return True
        same_table = self.table == other.table
        return same_table and (self.column is None or self.column == other.column)

    def overlaps(self, other: Scope) -> bool:
        return self.contains(other) or other.contains(self)


@dataclass(frozen=True, order=True)
class Right:
    """One privilege flag on one scope, with or without grant option."""

    privilege: str
    scope: Scope
    grant_option: bool

    def covered_by(self, held: frozenset[Right]) -> bool:
        return any(
            other.privilege == self.privilege
            and other.scope.contains(self.scope)
            and other.grant_option >= self.grant_option
            for other in held
        )


@dataclass(frozen=True)
class Grant:
    """A parsed `<privileges> ON <target> [WITH GRANT OPTION]`."""

    privileges: tuple[str, ...]
    scope: Scope
    grant_option: bool
    best_effort: bool


@dataclass(frozen=True)
class Difference:
    """What a desired set and a live set disagree on, right by right."""

    missing: frozenset[Right]
    extra: frozenset[Right]
    partial_revokes: frozenset[Right]

    @property
    def in_sync(self) -> bool:
        return not (self.missing or self.extra or self.partial_revokes)


class PrivilegeTree:
    """`system.privileges` from one server: levels, groups and aliases."""

    def __init__(self, rows):
        self.levels: dict[str, str | None] = {}
        self.children: dict[str, list[str]] = defaultdict(list)
        self.names: dict[str, str] = {}
        for name, aliases, level, parent in rows:
            self.levels[name] = level
            if parent is not None:
                self.children[parent].append(name)
            for spelling in (name, *aliases):
                self.names[_spelling(spelling)] = name

    @classmethod
    def read(cls, client) -> PrivilegeTree:
        return cls(client.query(_PRIVILEGES).result_rows)

    def canonical(self, privilege: str) -> str:
        """The stored name for a privilege or one of its aliases; KeyError when unknown."""
        return self.names[_spelling(privilege)]

    def flags(self, privilege: str, kind: str) -> frozenset[str]:
        """The flags `privilege` grants on a scope of this kind."""
        name = self.canonical(privilege)
        own = {name} if _level_allowed(self.levels[name], kind) else set()
        return frozenset(own.union(*(self.flags(child, kind) for child in self.children[name])))

    def leaves(self) -> list[str]:
        """Every privilege that carries a flag of its own."""
        return sorted(name for name, level in self.levels.items() if level is not None)


def parse_grant(text: str) -> Grant:
    """Parse a grant as access.yaml or 0.8's bootstrap SQL writes it."""
    match = _GRANT.match(" ".join(text.split()))
    if not match:
        raise ValueError(f"not a grant: {text!r}")
    privileges = tuple(part.strip() for part in match["privileges"].split(","))
    return Grant(
        privileges, _target(match["target"]), bool(match["option"]), bool(match["current"])
    )


def desired_rights(tree: PrivilegeTree, grants: list[Grant]) -> frozenset[Right]:
    """The rights a list of grants asks for."""
    rights = set()
    for grant in grants:
        for privilege in grant.privileges:
            for flag in tree.flags(privilege, grant.scope.kind):
                rights.add(Right(flag, grant.scope, grant.grant_option))
    return frozenset(rights)


def live_rights(tree: PrivilegeTree, rows: list[dict]) -> tuple[frozenset, frozenset]:
    """Rights held and partial revokes, from `system.grants` rows for one grantee."""
    held, revoked = set(), set()
    for row in rows:
        scope = Scope(row["database"], row["table"], row.get("access_object") or "", row["column"])
        for flag in tree.flags(row["access_type"], scope.kind):
            right = Right(flag, scope, bool(row["grant_option"]))
            (revoked if row["is_partial_revoke"] else held).add(right)
    return frozenset(held), frozenset(revoked)


def admin_rights(client, tree: PrivilegeTree) -> tuple[frozenset, frozenset]:
    """What the logged-in login holds by its own grants, and what is revoked inside them."""
    result = client.query("SELECT * FROM system.grants WHERE user_name = currentUser()")
    rows = [dict(zip(result.column_names, row)) for row in result.result_rows]
    return live_rights(tree, rows)


def held_in_full(right: Right, held: frozenset[Right], revoked: frozenset[Right]) -> bool:
    """Held with grant option, and nothing inside it revoked.

    ClickHouse Cloud's admin, for one, holds `SELECT ON *.*` with `system.zookeeper` revoked.
    """
    inside = (other for other in revoked if other.privilege == right.privilege)
    covered = Right(right.privilege, right.scope, True).covered_by(held)
    return covered and not any(right.scope.contains(other.scope) for other in inside)


def gives_in_full(client, text: str) -> bool:
    """Whether the logged-in admin can pass on every right of a grant, with grant option."""
    tree = PrivilegeTree.read(client)
    held, revoked = admin_rights(client, tree)
    rights = desired_rights(tree, [parse_grant(text)])
    return all(held_in_full(right, held, revoked) for right in rights)


def compare(desired: frozenset[Right], live: tuple[frozenset, frozenset]) -> Difference:
    """Missing and extra rights, each judged by coverage so folding cancels out."""
    held, revoked = live
    missing = frozenset(right for right in desired if not right.covered_by(held))
    extra = frozenset(right for right in held if not right.covered_by(desired))
    return Difference(missing, extra, revoked)


def _spelling(name: str) -> str:
    return " ".join(name.upper().split())


def _level_allowed(level: str | None, kind: str) -> bool:
    if level is None:
        return False
    if kind == "global":
        return True
    if kind == "source":
        # A named object that isn't a database or table: a source such as REMOTE, or a user,
        # as in ClickHouse Cloud's admin's `ALTER USER` with its console user revoked.
        return level != "GLOBAL" and level not in _DATABASE_LEVELS
    return level in (_DATABASE_LEVELS if kind == "database" else _TABLE_LEVELS)


def _target(text: str) -> Scope:
    if "." not in text:
        return Scope(source=text.upper()) if text != "*" else Scope()
    database, table = text.split(".", 1)
    if database == "*":
        return Scope()
    return Scope(database, None if table == "*" else table)
