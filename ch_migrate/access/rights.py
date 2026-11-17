"""Rights: what a grant means, one privilege flag at a time (design note, 2.7).

ClickHouse folds what it stores in `system.grants`. A group may be stored as itself or as
its children, a narrower grant is absorbed by a broader one, and an alias is stored under
its canonical name. So grants are never compared as text. Both sides become rights, one per
privilege flag, scope and grant option, using the `system.privileges` tree of the server
being compared:

- a privilege with a level carries a flag of its own, and one without is a group;
- a privilege means its own flag plus its descendants' flags, keeping only the flags whose
  level the target's scope allows.

What a user or role holds is read as the server reads it: at each scope the nearest row
decides, and a row with `is_partial_revoke = 1` takes a right away inside a broader grant.

`READ` and `WRITE` on one source, or on every source at `*.*`, are one grant to ClickHouse
unless `access_control_improvements.enable_read_write_grants` is on. That setting is off by
default and can't be read from SQL. With it off, `READ ON S3` reads back as `READ` and
`WRITE`: at once where users and roles are stored in Keeper or the grant ran ON CLUSTER, and
after a restart elsewhere. So a declared right in one direction covers the other direction
too: that one is never surplus (design note, Decision 12).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from ch_migrate.access.model import Grant, Target
from ch_migrate.access.privileges import PrivilegeTree

_SCOPE_WORDS = {
    "global": "`*.*`",
    "database": "a database",
    "table": "a table",
    "source": "a source",
}
# The two directions of a source grant, each mapped to the other.
_OTHER_DIRECTION = {"READ": "WRITE", "WRITE": "READ"}


@dataclass(frozen=True)
class Scope:
    """Where a right applies: `*.*`, `db.*`, `db.table`, a column, or a data source."""

    database: str | None = None
    table: str | None = None
    column: str | None = None
    source: str | None = None

    @classmethod
    def of(cls, target: Target) -> Scope:
        return cls(target.database, target.table, None, target.source)

    def contains(self, other: Scope) -> bool:
        """True when everything `other` covers is inside this scope."""
        if self.depth == 0:
            return True
        if self.source is not None or other.source is not None:
            return self.source == other.source
        if self.database != other.database:
            return False
        if self.table is None:
            return True
        return self.table == other.table and self.column in (None, other.column)

    @property
    def depth(self) -> int:
        """How narrow the scope is: 0 for `*.*`, up to 3 for a column."""
        if self.column is not None:
            return 3
        return {"global": 0, "database": 1, "source": 1, "table": 2}[self.kind]

    @property
    def kind(self) -> str:
        """`global`, `database`, `table` or `source`: which privilege levels can apply."""
        return self.target.scope

    @property
    def target(self) -> Target:
        """The grant target this scope is on; a column is on its table."""
        return Target(self.database, self.table, self.source)


@dataclass(frozen=True)
class Right:
    """One privilege flag on one scope, with or without grant option."""

    flag: str
    scope: Scope
    grant_option: bool = False


@dataclass(frozen=True)
class StoredRow:
    """One flag of a `system.grants` row: a grant, or a revoke inside a broader grant."""

    flag: str
    scope: Scope
    grant_option: bool = False
    revoke: bool = False

    def decides(self, option: bool) -> bool:
        """Whether the row says anything about the right itself, or about its grant option.

        A grant with grant option gives both. A revoke takes both away, unless it carries
        `grant_option`, which takes away only the option.
        """
        if self.revoke:
            return option or not self.grant_option
        return self.grant_option or not option


@dataclass(frozen=True)
class Surplus:
    """A held row that no desired right covers: the whole right, or only its grant option."""

    row: StoredRow
    option_only: bool = False

    def undermines(self, desired: Iterable[Right]) -> bool:
        """Whether revoking it takes a desired right with it: the same flag, strictly inside.

        The narrower grant can't be granted first, because the broader one absorbs it. So
        this is the case `apply` replaces the whole declared set for (design note, 2.5).
        """
        held = self.row
        return any(
            right.flag == held.flag
            and right.scope != held.scope
            and held.scope.contains(right.scope)
            and (right.grant_option or not self.option_only)
            for right in desired
        )


@dataclass(frozen=True)
class Holdings:
    """Every `system.grants` row of one user or role, one flag each."""

    rows: tuple[StoredRow, ...] = ()

    def holds(self, right: Right) -> bool:
        """Whether every part of a right is held, with its grant option if it has one."""
        if not self._decided(right, option=False):
            return False
        return not right.grant_option or self._decided(right, option=True)

    def surplus(self, desired: frozenset[Right]) -> tuple[Surplus, ...]:
        """The grant rows that the desired rights don't cover.

        A desired `READ` or `WRITE` covers the other direction on the same scope too.
        """
        found = []
        covers = _both_directions(desired)
        for row in self.rows:
            if row.revoke:
                continue
            covering = [
                right
                for right in covers
                if right.flag == row.flag and right.scope.contains(row.scope)
            ]
            if not covering:
                found.append(Surplus(row))
            elif row.grant_option and not any(right.grant_option for right in covering):
                found.append(Surplus(row, option_only=True))
        return tuple(found)

    def _decided(self, right: Right, *, option: bool) -> bool:
        """The nearest row at or above the scope grants it, and no revoke sits inside it."""
        nearest: StoredRow | None = None
        for row in self.rows:
            if row.flag != right.flag or not row.decides(option):
                continue
            if row.revoke and row.scope != right.scope and right.scope.contains(row.scope):
                return False
            if row.scope.contains(right.scope):
                if nearest is None or row.scope.depth > nearest.scope.depth:
                    nearest = row
        return nearest is not None and not nearest.revoke


def rights_of(tree: PrivilegeTree, grant: Grant) -> frozenset[Right]:
    """The rights a grant asks for; a name the server doesn't know asks for nothing."""
    scope = Scope.of(grant.target)
    flags: set[str] = set()
    for name in grant.privileges:
        privilege = tree.canonical(name)
        if privilege is not None:
            flags.update(tree.flags(privilege, scope.kind))
    return frozenset(Right(flag, scope, grant.grant_option) for flag in flags)


def grant_problems(tree: PrivilegeTree, grant: Grant) -> list[str]:
    """Why the server would reject a grant: a name it doesn't know, or one with no flag here."""
    problems = []
    kind = grant.target.scope
    for name in grant.privileges:
        privilege = tree.canonical(name)
        if privilege is None:
            problems.append(f"the server has no privilege named {name}")
        elif not tree.flags(privilege, kind):
            problems.append(f"the server can't grant {privilege} on {_SCOPE_WORDS[kind]}")
    return problems


def stored_rows(tree: PrivilegeTree, row: Mapping[str, Any]) -> list[StoredRow]:
    """One `system.grants` row as the flags it stands for.

    A row the tree can't expand keeps its own name, so it still counts as held and is
    never skipped. That covers a grant on something other than a database, table or data
    source, such as `TABLE ENGINE ON MergeTree`: its `access_object` reads as the scope.
    """
    scope = Scope(row["database"], row["table"], row["column"], row["access_object"] or None)
    name = str(row["access_type"])
    known = tree.canonical(name)
    flags = tree.flags(known, scope.kind) if known is not None else frozenset()
    option, revoke = bool(row["grant_option"]), bool(row["is_partial_revoke"])
    return [StoredRow(flag, scope, option, revoke) for flag in sorted(flags or {name})]


def fold(tree: PrivilegeTree, flags: Iterable[str], kind: str) -> tuple[str, ...]:
    """The fewest privilege names that mean exactly these flags on a scope of this kind.

    A group is named only when the set holds every flag the group has there. So a revoke
    written with these names never takes away more than the set: narrowing `ALTER` to
    `ALTER UPDATE` revokes the other `ALTER` leaves, never `ALTER` itself (design note, 2.7).

    On a data source the flags stay as they are: `READ, WRITE ON S3` is never `ALL ON S3`,
    which the server doesn't take.
    """
    if kind == "source":
        return tuple(sorted(flags))
    folding = _Folding(tree, kind, set(flags))
    for root in tree.roots:
        folding.visit(root)
    return tuple(sorted([*folding.names, *folding.left]))


def one_direction(grants: Iterable[Grant]) -> list[Grant]:
    """Where one user's or role's grants give `READ` or `WRITE` but not both.

    One grant for each source, or `*.*`, naming the one direction given there.
    """
    directions: dict[Target, set[str]] = {}
    for grant in grants:
        named = set(_OTHER_DIRECTION).intersection(grant.privileges)
        if named and grant.target.scope in ("source", "global"):
            directions.setdefault(grant.target, set()).update(named)
    return [Grant(tuple(named), target) for target, named in directions.items() if len(named) == 1]


@dataclass
class _Folding:
    """A walk down the privilege tree, naming each group the flags left hold all of."""

    tree: PrivilegeTree
    kind: str
    left: set[str]
    names: list[str] = field(default_factory=list)

    def visit(self, privilege: str) -> None:
        flags = self.tree.flags(privilege, self.kind)
        if flags and flags <= self.left:
            self.names.append(privilege)
            self.left -= flags
            return
        for child in self.tree.children.get(privilege, ()):
            self.visit(child)


def _both_directions(rights: frozenset[Right]) -> frozenset[Right]:
    """The rights, plus the other direction of each `READ` or `WRITE`, on the same scope."""
    others = {
        Right(_OTHER_DIRECTION[right.flag], right.scope, right.grant_option)
        for right in rights
        if right.flag in _OTHER_DIRECTION
    }
    return rights | others
