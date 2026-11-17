"""`access show`: what the server holds for some users and roles, in access.yaml's form.

The output is YAML that `access validate` accepts. Copied into access.yaml, it declares what
the server already holds, which is the starting point for adopting existing users and roles:

- a user or role prints under `users:` or `roles:`, with its grants, the roles it holds and,
  for a user, its default roles when they aren't all of them;
- the migration role prints under `migration_role:`, with its profile and what it holds
  beyond it;
- the project's own database is written `{db}`, so the file suits every environment;
- no password is ever read or shown.

What access.yaml can't declare is left out with a warning: a column grant, a partial
revoke, a role held with admin option, and a grant on something other than a database, a
table or a data source, such as `TABLE ENGINE ON MergeTree`.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Iterable

import yaml

from ch_migrate.access.grants import SOURCE_PRIVILEGES
from ch_migrate.access.live import LiveEntity, LiveState, Wanted
from ch_migrate.access.model import AccessSpec, Grant, Target
from ch_migrate.access.planner import Entity, surplus_grant
from ch_migrate.access.rights import Scope, StoredRow, rights_of
from ch_migrate.access.server import AccessError
from ch_migrate.access.settings import ProjectSettings

_HEADER = (
    "# What the server holds for {env}, in access.yaml's form (`ch-migrate access show`).\n"
    "# Passwords are never shown. For ch-migrate to create a user, add its\n"
    "# `password: {{secret: NAME}}`.\n"
)


@dataclass(frozen=True)
class Showing:
    """What `access show` was asked for: names given, or else everything declared."""

    settings: ProjectSettings
    spec: AccessSpec
    given: tuple[str, ...] = ()

    def check(self) -> None:
        """Refuse a name that access.yaml can't declare, before anything is read."""
        reasons = {
            self.settings.admin_user: "config.yaml's admin_user, which ch-migrate never changes",
            self.spec.migration.user: "the migration user, which `bootstrap` manages",
        }
        for name in self.given:
            if name in reasons:
                raise AccessError(f"{name} is {reasons[name]}, so access.yaml can't declare it.")

    @property
    def wanted(self) -> Wanted:
        return Wanted(self.names)

    def unsettled(self, live: LiveState) -> bool:
        """Whether a name isn't on the server, which a replica that is behind would also show."""
        return any(self._absent(live, name) for name in self.expected)

    @property
    def expected(self) -> tuple[str, ...]:
        """The names that should be on the server: all but those declared `absent: true`."""
        absent = {role.name for role in self.spec.roles if role.absent}
        absent |= {user.name for user in self.spec.users if user.absent}
        return tuple(name for name in self.names if name in self.given or name not in absent)

    @property
    def names(self) -> tuple[str, ...]:
        if self.given:
            return tuple(dict.fromkeys(self.given))
        declared = [role.name for role in self.spec.roles] + [user.name for user in self.spec.users]
        return tuple(dict.fromkeys(declared))

    def _absent(self, live: LiveState, name: str) -> bool:
        return live.find("role", name) is None and live.find("user", name) is None


@dataclass(frozen=True)
class Shown:
    """The YAML text, and a warning for each thing access.yaml can't declare."""

    text: str
    warnings: tuple[str, ...] = ()


def show_access(showing: Showing, live: LiveState) -> Shown:
    """What `live` holds for the names asked for, as access.yaml text."""
    page = _Page(showing, live)
    for name in showing.names:
        page.add(name)
    header = _HEADER.format(env=showing.spec.environment)
    return Shown(header + page.text(), tuple(page.warnings))


@dataclass
class _Page:
    """The document while it is being filled in."""

    showing: Showing
    live: LiveState
    migration: dict[str, Any] | None = None
    roles: dict[str, Any] = field(default_factory=dict)
    users: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def add(self, name: str) -> None:
        role, user = self.live.find("role", name), self.live.find("user", name)
        if role is None and user is None:
            self._not_found(name)
        if role is not None and name == self.showing.spec.migration.role:
            self.migration = self._migration_block(Entity("role", name), role)
        elif role is not None:
            self.roles[name] = self._block(Entity("role", name), role)
        if user is not None:
            self.users[name] = self._block(Entity("user", name), user)

    def text(self) -> str:
        sections = {"migration_role": self.migration, "roles": self.roles, "users": self.users}
        document = {key: value for key, value in sections.items() if value}
        return yaml.dump(
            document,
            Dumper=_Dumper,
            sort_keys=False,
            default_flow_style=False,
            allow_unicode=True,
            width=10_000,
        )

    def _not_found(self, name: str) -> None:
        if name in self.showing.given:
            raise AccessError(f"The server has no user or role named {name}.")
        if name in self.showing.expected:
            self.warnings.append(f"{name} is declared but isn't on the server, so it isn't shown.")

    def _migration_block(self, entity: Entity, held: LiveEntity) -> dict[str, Any]:
        """The profile, and the grants the role holds beyond it."""
        migration, tree = self.showing.spec.migration, self.live.tree
        profile = frozenset(
            right for grant in migration.profile.grants for right in rights_of(tree, grant)
        )
        beyond = [item.row for item in held.holdings.surplus(profile)]
        for membership in held.memberships:
            self.warnings.append(
                f"{entity} holds role {membership.role}, which migration_role can't declare."
            )
        grants = self._grants(entity, beyond)
        return {"profile": migration.profile.name, **({"grants": grants} if grants else {})}

    def _block(self, entity: Entity, held: LiveEntity) -> dict[str, Any]:
        """A role's `grants` then `roles`; a user's `roles`, `default_roles`, then `grants`."""
        roles = [membership.role for membership in held.memberships]
        defaults = [membership.role for membership in held.memberships if membership.default]
        for membership in held.memberships:
            if membership.admin_option:
                self.warnings.append(
                    f"{entity} holds role {membership.role} with admin option, "
                    "which access.yaml can't declare."
                )
        grants = self._grants(entity, held.holdings.rows)
        block: dict[str, Any] = {"grants": grants} if grants and entity.kind == "role" else {}
        if roles:
            block["roles"] = roles
        if entity.kind == "user" and defaults != roles:
            block["default_roles"] = defaults
        if grants and entity.kind == "user":
            block["grants"] = grants
        return block

    def _grants(self, entity: Entity, rows: Iterable[StoredRow]) -> list[str]:
        """Grant entries for the rows access.yaml can declare; a warning for the others."""
        groups: dict[tuple[Scope, bool, bool], list[str]] = {}
        for row in rows:
            groups.setdefault((row.scope, row.grant_option, row.revoke), []).append(row.flag)
        entries = []
        for (scope, option, revoke), flags in groups.items():
            grant = replace(surplus_grant(self.live.tree, scope, flags), grant_option=option)
            if revoke or scope.column is not None or _other_object(scope, flags):
                self.warnings.append(_undeclarable(entity, grant, revoke))
            else:
                entries.append(str(replace(grant, target=self._placeholder(grant.target))))
        return sorted(entries)

    def _placeholder(self, target: Target) -> Target:
        """The project's own database as `{db}`, as access.yaml writes it."""
        if target.database != self.showing.settings.database:
            return target
        return replace(target, database="{db}")


class _Dumper(yaml.SafeDumper):
    """Block style with list items indented under their key, as access.yaml is written."""

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> Any:
        return super().increase_indent(flow, False)


def _other_object(scope: Scope, flags: Iterable[str]) -> bool:
    """A grant on a name that isn't a data source: only READ and WRITE take one of those."""
    return scope.source is not None and not SOURCE_PRIVILEGES.issuperset(flags)


def _undeclarable(entity: Entity, grant: Grant, revoke: bool) -> str:
    if revoke:
        what = "the grant option for " if grant.grant_option else ""
        plain = replace(grant, grant_option=False)
        return (
            f"{entity} has a partial revoke of {what}{plain}, which access.yaml can't declare. "
            "`access plan` shows the grant that clears it."
        )
    if grant.target.source is not None:
        return f"{entity} holds {grant}, which access.yaml can't declare, so it is left out."
    return f"{entity} holds {grant}: column grants aren't in v1, so it is left out."
