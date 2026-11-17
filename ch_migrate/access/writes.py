"""Render access changes using sql/apply.sql; quote names and bind passwords.

Local-storage cluster writes use ON CLUSTER with wait_end_of_query. Best-effort grants
use CURRENT GRANTS elsewhere, or one plain grant per privilege ON CLUSTER.
Scope narrowing replaces the whole declared set, then restores grant options in a second
statement when needed. Keep held best-effort access; Unkept refuses a replace that cannot
preserve a partial revoke exactly. The migration identity is never replaced (design 2.5).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any, Mapping

from ch_migrate.access.live import LiveEntity, LiveState
from ch_migrate.access.model import AccessSpec, Grant, Role, Target, User
from ch_migrate.access.planner import Change, Entity, declared_entities, surplus_grant
from ch_migrate.access.rights import Right, Scope, rights_of
from ch_migrate.access.templates import statement

# Sent with every ON CLUSTER statement. ClickHouse stops waiting for a host before the
# client's 60-second read timeout (`server.connect`), so a slow host is an error with a code.
ON_CLUSTER_SETTINGS = {
    "wait_end_of_query": 1,
    "distributed_ddl_output_mode": "throw",
    "distributed_ddl_task_timeout": 45,
}
# The changes that set a password, bound as {password:String}.
_PASSWORD_ACTIONS = frozenset({"create_user", "reset_password"})
# A data source, or another object the server names, that is one word is written bare, as
# in `READ ON REMOTE`.
_WORD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# A column grant, as the planner writes one: `SELECT(column)`.
_COLUMN = re.compile(r"^(?P<privilege>[^(]+)\((?P<column>.+)\)$")


@dataclass(frozen=True)
class Write:
    """One statement, the parameters the server binds, and the settings it is sent with."""

    sql: str
    parameters: Mapping[str, str] = field(default_factory=dict, repr=False)
    settings: Mapping[str, Any] = field(default_factory=dict)
    gives: Grant | None = None  # a best-effort statement's grant, which a refusal names


@dataclass(frozen=True)
class Writing:
    """What the statements are written from: the declaration, one read, and passwords."""

    spec: AccessSpec
    live: LiveState
    cluster: str | None = None  # ON CLUSTER this: users and roles are on each node's disk
    passwords: Mapping[str, str] = field(default_factory=dict, repr=False)  # by user name

    def declared(self, entity: Entity) -> Role | User:
        """The declared user or role a change is for."""
        kind = User if entity.kind == "user" else Role
        return next(
            declared
            for declared in declared_entities(self.spec)
            if isinstance(declared, kind) and declared.name == entity.name
        )


class Unkept(Exception):
    """A replace isn't sent: it can't keep exactly what is held of a best-effort grant (2.5).

    The message says why, worded to follow "because".
    """


def writes_for(change: Change, writing: Writing) -> tuple[Write, ...]:
    """The statements that make one change; none for a line that only notes something."""
    if change.action == "replace":
        return _replace(change, writing)
    if change.action == "best_effort" and change.grant is not None:
        return best_effort_writes(change.entity.name, change.grant, writing.cluster)
    if change.marker not in "+-":
        return ()
    write = _write(writing.cluster, _template(change.action, change.grant), _values(change))
    if change.action in _PASSWORD_ACTIONS:
        write = replace(write, parameters={"password": writing.passwords[change.entity.name]})
    return (write,)


def best_effort_writes(grantee: str, grant: Grant, cluster: str | None) -> tuple[Write, ...]:
    """How a best-effort grant is sent; each statement may be refused, as only a warning.

    With no cluster: one `GRANT CURRENT GRANTS(...)`. ON CLUSTER: one plain GRANT for each
    privilege, since ClickHouse refuses CURRENT GRANTS there (Decision 12). Each statement
    `gives` the part of the grant it is for.
    """
    if cluster is None:
        forms = [("best_effort", grant)]
    else:
        forms = [("grant", replace(grant, privileges=(name,))) for name in grant.privileges]
    return tuple(
        replace(
            _write(cluster, _template(action, part), _granting(grantee, part)),
            gives=part,
        )
        for action, part in forms
    )


def grant_clause(grant: Grant) -> str:
    """`<privileges> ON <target>`, written as every statement here writes it."""
    return _clauses([grant])


def quote(name: str) -> str:
    """A name as a quoted identifier."""
    return "`" + name.replace("\\", "\\\\").replace("`", "\\`") + "`"


def _replace(change: Change, writing: Writing) -> tuple[Write, ...]:
    """The whole declared set in one statement, then the grant options it carries."""
    entity = change.entity
    if entity.kind == "role" and entity.name == writing.spec.migration.role:
        raise ValueError(f"{entity} is only ever added to, never replaced (design note, 2.2)")
    plain, optioned = _replacement(writing, entity)
    if not plain:
        # Nothing declared is held now or required, so there is no gap to avoid.
        return tuple(write for inner in change.covers for write in writes_for(inner, writing))
    grantee = quote(entity.name)
    writes = [_write(writing.cluster, "replace", {"grants": _clauses(plain), "grantee": grantee})]
    if optioned:
        values = {"grants": _clauses(optioned), "grantee": grantee}
        writes.append(_write(writing.cluster, "grant_with_grant_option", values))
    return tuple(writes)


def _replacement(writing: Writing, entity: Entity) -> tuple[list[Grant], list[Grant]]:
    """The declared grants a replace gives, without grant option, and those that carry it."""
    held = writing.live.find(entity.kind, entity.name) or LiveEntity()
    plain, optioned = [], []
    for grant in writing.declared(entity).grants:
        parts = _held_parts(writing.live, held, grant) if grant.best_effort else [grant]
        for part in parts:
            plain.append(replace(part, grant_option=False))
            if part.grant_option:
                optioned.append(part)
    return plain, optioned


def _held_parts(live: LiveState, held: LiveEntity, grant: Grant) -> list[Grant]:
    """What a user or role holds of a best-effort grant now: none, some or all of it.

    That is each flag it holds on the grant's whole scope, and each held row inside the
    scope for another of its flags, such as `system.tables` of `SELECT ON system.*`. A part
    keeps its grant option only where every flag held there has one. Raises `Unkept` when a
    revoke inside the scope means no set of grants keeps what is held.
    """
    _refuse_revokes_inside(live, held, grant)
    holdings, scope = held.holdings, Scope.of(grant.target)
    flags = {right.flag for right in rights_of(live.tree, grant)}
    parts = {scope: {flag for flag in flags if holdings.holds(Right(flag, scope))}}
    for row in holdings.rows:
        inside = row.scope != scope and scope.contains(row.scope) and not row.revoke
        if inside and row.flag in flags - parts[scope]:
            parts.setdefault(row.scope, set()).add(row.flag)
    return [
        replace(
            surplus_grant(live.tree, where, kept),
            grant_option=grant.grant_option
            and all(holdings.holds(Right(flag, where, grant_option=True)) for flag in kept),
            best_effort=True,
        )
        for where, kept in parts.items()
        if kept
    ]


def _refuse_revokes_inside(live: LiveState, held: LiveEntity, grant: Grant) -> None:
    """Raise `Unkept` if, for one of the grant's flags, a revoke sits inside its scope.

    The grant above that revoke, at the scope or above it, is held but for the revoke. A
    replace can't write "all but", so it would drop the rest, such as `system.tables` of
    `SELECT ON system.*` held less `system.users`. A revoke of only the grant option counts
    where the grant carries one.
    """
    scope = Scope.of(grant.target)
    flags = {right.flag for right in rights_of(live.tree, grant)}
    revoked = sorted(
        ("the grant option for " if row.grant_option else "")
        + str(surplus_grant(live.tree, row.scope, [row.flag]))
        for row in held.holdings.rows
        if row.revoke
        and row.flag in flags
        and row.scope != scope
        and scope.contains(row.scope)
        and row.decides(grant.grant_option)
    )
    if revoked:
        raise Unkept(
            f"what it holds of the best-effort grant {grant} has {', '.join(revoked)} revoked "
            "inside it, which a replace can't keep, so it keeps the access it has"
        )


def _template(action: str, grant: Grant | None) -> str:
    """The statement's name in `sql/apply.sql`: a grant with grant option has its own."""
    if action in ("grant", "best_effort") and grant is not None and grant.grant_option:
        return action + "_with_grant_option"
    return action


def _values(change: Change) -> dict[str, str]:
    name = quote(change.entity.name)
    values = {"name": name, "grantee": name}
    if change.grant is not None:
        values["grants"] = _clauses([change.grant])
    if change.action == "set_default_roles":
        values["roles"] = _default_roles(change.roles)
    elif change.roles:
        values["role"] = quote(change.roles[0])
    return values


def _granting(grantee: str, grant: Grant) -> dict[str, str]:
    return {"grantee": quote(grantee), "grants": _clauses([grant])}


def _write(cluster: str | None, template: str, values: Mapping[str, str]) -> Write:
    """A statement from `sql/apply.sql`, ON CLUSTER and sent with its settings where needed."""
    on_cluster = f" ON CLUSTER {quote(cluster)}" if cluster else ""
    sql = statement("apply", template, {**values, "cluster": on_cluster})
    return Write(sql, settings=dict(ON_CLUSTER_SETTINGS) if cluster else {})


def _clauses(grants: list[Grant]) -> str:
    """`<privileges> ON <target>` for each grant, joined: one GRANT takes several."""
    return ", ".join(
        f"{', '.join(_privilege(name) for name in grant.privileges)} ON {_target(grant.target)}"
        for grant in grants
    )


def _privilege(name: str) -> str:
    column = _COLUMN.match(name)
    if column is None:
        return name
    return f"{column['privilege']}({quote(column['column'])})"


def _target(target: Target) -> str:
    if target.source is not None:
        return target.source if _WORD.match(target.source) else quote(target.source)
    database = quote(target.database) if target.database is not None else "*"
    table = quote(target.table) if target.table is not None else "*"
    return f"{database}.{table}"


def _default_roles(roles: tuple[str, ...] | None) -> str:
    """`ALL` when every role a user holds is a default role, `NONE` for none of them."""
    if roles is None:
        return "ALL"
    return ", ".join(quote(role) for role in roles) or "NONE"
