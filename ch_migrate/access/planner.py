"""Compare a declaration with live access; this module never writes.

Declared users and roles converge; migration access is additive and undeclared entities
are untouched. Scope narrowing needs a whole-set replace because broader grants absorb
narrower ones. Best-effort shortfalls and implicit privileges are not counted changes.
The shared action catalog fixes display text, markers and execution order (design 2.5).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Iterable

from ch_migrate.access.live import LiveEntity, LiveState, Wanted
from ch_migrate.access.model import AccessSpec, Grant, Role, User
from ch_migrate.access.privileges import PrivilegeTree
from ch_migrate.access.rights import Right, Scope, Surplus, fold, grant_problems, rights_of

# Marker and text, in execution order. Bootstrap alone adds create_database; apply adds
# password resets and best-effort tries. Both use this catalog's order.
_ACTIONS = {
    "create_database": ("+", "create {entity} if it doesn't exist"),
    "adopt": ("~", "adopt {entity}: it exists on the server, and access.yaml now manages it"),
    "create_role": ("+", "create {entity}"),
    "create_user": ("+", "create {entity}"),
    "reset_password": ("+", "reset the password of {entity}"),
    "grant_role": ("+", "grant role {role} to {entity}"),
    "grant": ("+", "grant {grant} to {entity}"),
    "replace": (
        "-",
        "replace the grants of {entity} with its declared set in one statement, "
        "instead of: {covers}",
    ),
    "best_effort": ("+", "grant {entity} as much of {grant} as the admin holds (best-effort)"),
    "set_default_roles": ("+", "set the default roles of {entity} to {roles}"),
    "revoke": ("-", "revoke {grant} from {entity}"),
    "revoke_grant_option": ("-", "revoke the grant option for {grant} from {entity}"),
    "revoke_role": ("-", "revoke role {role} from {entity}"),
    "revoke_admin_option": ("-", "revoke the admin option for role {role} from {entity}"),
    "drop_user": ("-", "drop {entity}"),
    "drop_role": ("-", "drop {entity}"),
    "extra": ("!", "{entity} holds {grant} beyond its profile"),
    "extra_grant_option": ("!", "{entity} holds the grant option for {grant} beyond its profile"),
    "extra_role": ("!", "{entity} holds role {role} beyond its profile"),
}
ACTION_ORDER = {action: index for index, action in enumerate(_ACTIONS)}
# The changes that put a row on the server. A replica that is behind shows the same gap.
_ADDITIONS = frozenset({"create_role", "create_user", "grant_role", "grant"})


@dataclass(frozen=True)
class Entity:
    """A user or a role, by name."""

    kind: str  # "user" or "role"
    name: str

    def __str__(self) -> str:
        return f"{self.kind} {self.name}"


@dataclass(frozen=True)
class Change:
    """One line of the plan: something `apply` would do, or a note on what it leaves alone."""

    action: str  # a key of _ACTIONS
    entity: Entity
    grant: Grant | None = None  # the privileges and target of a grant, revoke or extra
    # The role of a membership change. For default roles: the roles, or None for all.
    roles: tuple[str, ...] | None = None
    covers: tuple[Change, ...] = ()  # the grants and revokes a replace stands in for

    @property
    def marker(self) -> str:
        return _ACTIONS[self.action][0]

    def __str__(self) -> str:
        return _ACTIONS[self.action][1].format(
            entity=self.entity,
            grant=self.grant,
            role=self.roles[0] if self.roles else "",
            roles=_default_roles_text(self.roles),
            covers="; ".join(str(change) for change in self.covers),
        )


@dataclass(frozen=True)
class Shortfall:
    """A best-effort grant that is missing or partly held. It is a warning, never a change.

    `grant` is the part that isn't held. With a `reason`, it is the grant as declared, which
    this server can't give at all.
    """

    entity: Entity
    grant: Grant
    reason: str = ""

    def __str__(self) -> str:
        if self.reason:
            return f"best-effort: {self.entity} can't be given {self.grant}: {self.reason}"
        return f"best-effort: {self.entity} lacks {self.grant}"


@dataclass(frozen=True)
class Plan:
    """What differs, in the order `apply` would change it."""

    changes: tuple[Change, ...] = ()
    shortfalls: tuple[Shortfall, ...] = ()
    # What makes the declaration impossible on this server. `plan` exits 2 for any of them.
    errors: tuple[str, ...] = ()
    # Whether the plan lacks a row a replica that is behind would also lack (2.5).
    missing_rows: bool = False

    @property
    def pending(self) -> tuple[Change, ...]:
        """The changes `apply` would make: every `+` and `-` line."""
        return tuple(change for change in self.changes if change.marker in "+-")

    @property
    def exit_code(self) -> int:
        """0 in sync, 1 changes pending, 2 error (design note, 2.5)."""
        if self.errors:
            return 2
        return 1 if self.pending else 0


def build_plan(spec: AccessSpec, live: LiveState) -> Plan:
    """Compare what one environment declares with one read of its server."""
    draft = _Draft(spec, live)
    _plan_migration(draft)
    for declared in declared_entities(spec):
        _plan_declared(draft, declared)
    return draft.plan()


def wanted(spec: AccessSpec) -> Wanted:
    """The users and roles a plan reads, and the data sources its grants name."""
    migration = spec.migration
    names = [migration.role, migration.user]
    grants = list(migration.all_grants)
    for declared in declared_entities(spec):
        names += [declared.name, *declared.roles]
        grants += declared.grants
    sources = {grant.target.source for grant in grants if grant.target.source is not None}
    return Wanted(tuple(dict.fromkeys(names)), tuple(sorted(sources)))


def declared_entities(spec: AccessSpec) -> tuple[Role | User, ...]:
    """The roles, then the users, that a plan converges: none in legacy mode (2.4)."""
    declared: tuple[Role | User, ...] = (*spec.roles, *spec.users)
    return () if spec.legacy else declared


def surplus_grant(tree: PrivilegeTree, scope: Scope, flags: Iterable[str]) -> Grant:
    """Flags held on one scope, written as a grant with the fewest privilege names."""
    names = fold(tree, flags, scope.kind)
    if scope.column is not None:
        names = tuple(f"{name}({scope.column})" for name in names)
    return Grant(names, scope.target)


@dataclass
class _Draft:
    """A plan while it is being built."""

    spec: AccessSpec
    live: LiveState
    changes: list[Change] = field(default_factory=list)
    shortfalls: list[Shortfall] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    absent: bool = False  # an error came from something not being on the server

    def subject(self, entity: Entity, grants: tuple[Grant, ...]) -> _Subject:
        found = self.live.find(entity.kind, entity.name)
        return _Subject(entity, grants, found or LiveEntity(), found is not None)

    def creatable_roles(self) -> set[str]:
        """The roles the plan creates if they are missing."""
        declared = () if self.spec.legacy else self.spec.roles
        return {self.spec.migration.role, *(role.name for role in declared if not role.absent)}

    def plan(self) -> Plan:
        changes = sorted(self.changes, key=lambda change: ACTION_ORDER[change.action])
        missing = self.absent or any(
            item.action in _ADDITIONS for change in changes for item in (change, *change.covers)
        )
        return Plan(tuple(changes), tuple(self.shortfalls), tuple(self.errors), missing)


@dataclass(frozen=True)
class _Subject:
    """A user or role being compared: its declared grants, and what the server holds."""

    entity: Entity
    grants: tuple[Grant, ...]
    held: LiveEntity  # empty when it isn't on the server
    exists: bool
    declared: Role | User | None = None  # None for the migration role


def _plan_migration(draft: _Draft) -> None:
    """The migration role and user: created and added to, never revoked from (2.2)."""
    migration = draft.spec.migration
    role = draft.subject(Entity("role", migration.role), migration.all_grants)
    if not role.exists:
        draft.changes.append(Change("create_role", role.entity))
    draft.changes += [*_missing_grants(draft, role), *_extras(draft.live.tree, role)]
    user = Entity("user", migration.user)
    found = draft.live.find("user", migration.user)
    if found is None:
        draft.changes.append(Change("create_user", user))
    if found is None or migration.role not in {held.role for held in found.memberships}:
        draft.changes.append(Change("grant_role", user, roles=(migration.role,)))


def _plan_declared(draft: _Draft, declared: Role | User) -> None:
    """One declared user or role: dropped if `absent`, else created or converged."""
    entity = Entity("user" if isinstance(declared, User) else "role", declared.name)
    subject = replace(draft.subject(entity, declared.grants), declared=declared)
    if declared.absent:
        if subject.exists:
            draft.changes.append(Change(f"drop_{entity.kind}", entity))
        return
    if not subject.exists and not _creatable(draft, subject):
        return
    changes = [
        *_memberships(draft, subject),
        *_converged_grants(draft, subject),
        *_default_roles(subject),
    ]
    if not subject.exists:
        draft.changes += [Change(f"create_{entity.kind}", entity), *changes]
    elif changes:
        draft.changes += [Change("adopt", entity), *changes]


def _creatable(draft: _Draft, subject: _Subject) -> bool:
    """A user with no password is never created: it would be a login anyone can use."""
    if not isinstance(subject.declared, User) or subject.declared.password is not None:
        return True
    draft.absent = True
    draft.errors.append(
        f"{subject.entity} isn't on the server and has no password, so it can't be created. "
        "Set `password: {secret: NAME}`, or create the user first."
    )
    return False


def _memberships(draft: _Draft, subject: _Subject) -> list[Change]:
    """Grant the declared roles that aren't held, and revoke the held ones not declared."""
    assert subject.declared is not None
    entity, declared = subject.entity, tuple(dict.fromkeys(subject.declared.roles))
    held = {membership.role: membership for membership in subject.held.memberships}
    changes = [
        Change("grant_role", entity, roles=(role,))
        for role in declared
        if role not in held and _role_exists(draft, subject, role)
    ]
    for role, membership in held.items():
        if role not in declared:
            changes.append(Change("revoke_role", entity, roles=(role,)))
        elif membership.admin_option:
            changes.append(Change("revoke_admin_option", entity, roles=(role,)))
    return changes


def _role_exists(draft: _Draft, subject: _Subject, role: str) -> bool:
    """A membership may name a role access.yaml doesn't declare, if the server has it (2.3)."""
    if role in draft.creatable_roles() or draft.live.find("role", role) is not None:
        return True
    draft.absent = True
    draft.errors.append(
        f"{subject.entity} is declared to hold role {role}, "
        "which access.yaml doesn't declare and the server doesn't have."
    )
    return False


def _converged_grants(draft: _Draft, subject: _Subject) -> list[Change]:
    """The grants and revokes that make the held rows match the declared grants exactly.

    If a revoke would take a declared right with it, the whole declared set is replaced in
    one statement instead, and the line says what that stands in for.
    """
    tree = draft.live.tree
    desired = _desired(tree, subject)
    surplus = subject.held.holdings.surplus(desired)
    changes = [*_missing_grants(draft, subject), *_revokes(tree, subject.entity, surplus)]
    if any(item.undermines(desired) for item in surplus):
        return [Change("replace", subject.entity, covers=tuple(changes))]
    return changes


def _missing_grants(draft: _Draft, subject: _Subject) -> list[Change]:
    """A grant for each required grant not fully held; a shortfall for a best-effort one."""
    changes = []
    for grant in subject.grants:
        problems = _problems(draft.live, grant)
        if problems:
            if grant.best_effort:
                draft.shortfalls.append(Shortfall(subject.entity, grant, "; ".join(problems)))
            else:
                draft.errors += [f"{subject.entity}: `{grant}`: {problem}." for problem in problems]
            continue
        lacking = _lacking(draft.live, subject, grant)
        if lacking is not None:
            if grant.best_effort:
                draft.shortfalls.append(Shortfall(subject.entity, lacking))
            else:
                changes.append(Change("grant", subject.entity, grant=lacking))
    return changes


def _problems(live: LiveState, grant: Grant) -> list[str]:
    """Why this server would reject a grant: its names are checked here, live (2.5)."""
    problems = grant_problems(live.tree, grant)
    source = grant.target.source
    if source is not None and source not in live.sources:
        problems.append(f"the server has no data source named {source}")
    elif source is not None and source != source.upper():
        problems.append(
            f"the server stores {source} under another name: "
            "write the source's own name, in upper case, such as REMOTE"
        )
    return problems


def _lacking(live: LiveState, subject: _Subject, grant: Grant) -> Grant | None:
    """The part of a grant that isn't fully held, or None when all of it is.

    A right the server grants every login, such as CLUSTER on Cloud, is held without a row.
    """
    holdings, tree = subject.held.holdings, live.tree
    missing = [
        right.flag
        for right in rights_of(tree, grant)
        if not holdings.holds(right) and not live.held_by_all(right)
    ]
    if not missing:
        return None
    return replace(grant, privileges=fold(tree, missing, grant.target.scope))


def _revokes(tree: PrivilegeTree, entity: Entity, surplus: tuple[Surplus, ...]) -> list[Change]:
    """One revoke for each scope's surplus flags, and one for each scope's grant options."""
    groups: dict[tuple[Scope, bool], list[str]] = {}
    for item in surplus:
        groups.setdefault((item.row.scope, item.option_only), []).append(item.row.flag)
    return [
        Change(
            "revoke_grant_option" if option_only else "revoke",
            entity,
            grant=surplus_grant(tree, scope, flags),
        )
        for (scope, option_only), flags in sorted(groups.items(), key=_group_order)
    ]


def _default_roles(subject: _Subject) -> list[Change]:
    """Set a user's default roles when they wouldn't match once its memberships do."""
    user = subject.declared
    if not isinstance(user, User):
        return []
    roles, held = set(user.roles), subject.held
    desired = roles if user.default_roles is None else set(user.default_roles)
    if held.default_all:
        after = roles - set(held.default_except)
    else:
        after = {membership.role for membership in held.memberships if membership.default} & roles
    if after == desired:
        return []
    return [Change("set_default_roles", subject.entity, roles=user.default_roles)]


def _extras(tree: PrivilegeTree, role: _Subject) -> list[Change]:
    """What the migration role holds beyond its profile: listed, and never revoked."""
    groups: dict[tuple[Scope, bool, bool], list[str]] = {}
    for item in role.held.holdings.surplus(_desired(tree, role)):
        key = (item.row.scope, item.option_only, item.row.grant_option)
        groups.setdefault(key, []).append(item.row.flag)
    extras = []
    for (scope, option_only, option), flags in sorted(groups.items(), key=_group_order):
        grant = replace(surplus_grant(tree, scope, flags), grant_option=option and not option_only)
        action = "extra_grant_option" if option_only else "extra"
        extras.append(Change(action, role.entity, grant=grant))
    held = (membership.role for membership in role.held.memberships)
    return [*extras, *(Change("extra_role", role.entity, roles=(name,)) for name in held)]


def _desired(tree: PrivilegeTree, subject: _Subject) -> frozenset[Right]:
    """Every right the declared grants ask for, required and best-effort alike."""
    rights: set[Right] = set()
    for grant in subject.grants:
        rights |= rights_of(tree, grant)
    return frozenset(rights)


def _group_order(group: tuple[tuple, list[str]]) -> tuple:
    (scope, *flags), _ = group
    return (str(scope.target), scope.column or "", *flags)


def _default_roles_text(roles: tuple[str, ...] | None) -> str:
    if roles is None:
        return "all its roles"
    return ", ".join(roles) or "none"
