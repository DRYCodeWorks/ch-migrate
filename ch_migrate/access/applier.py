"""Prepare, apply and replan access changes (design 2.5).

Preparation resolves every refusal and secret before any write. Execution retains old
access when replacements or role dependencies fail; an unsent replace also blocks drops.
Dependency-first replaces, default-role gating and statement accounting are enforced by
Outcome and _steps. Replanning uses the same reader and waits for partial writes to settle.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping

from ch_migrate.access.live import REPLICATED_STORAGE, LiveState, Reader
from ch_migrate.access.model import AccessSpec, Role, User
from ch_migrate.access.planner import (
    ACTION_ORDER,
    Change,
    Entity,
    Plan,
    build_plan,
    declared_entities,
    wanted,
)
from ch_migrate.access.server import (
    AccessError,
    hide_secrets,
    read_secret,
    server_errors,
    server_message,
)
from ch_migrate.access.settings import ProjectSettings
from ch_migrate.access.templates import statement
from ch_migrate.access.writes import Unkept, Write, Writing, writes_for

# What must have landed, for any user or role, before any revoke, replace or default-role
# change (2.5).
_BEFORE_REVOKES = frozenset(
    {
        "create_role",
        "create_user",
        "reset_password",
        "grant_role",
        "grant",
        "replace",
        "set_default_roles",
    }
)
# What can take access away, so it waits for those: the revokes, a replace, and default
# roles, which can leave a role the user keeps inactive at login.
_GATED = frozenset(
    {
        "revoke",
        "revoke_grant_option",
        "revoke_role",
        "revoke_admin_option",
        "replace",
        "set_default_roles",
    }
)
_DROPS = frozenset({"drop_user", "drop_role"})
# The changes that set a user's password. When one fails, the user may be someone else's, with
# their password, so it gets nothing more from this run.
_SETS_PASSWORD = {"create_user": "create", "reset_password": "password reset"}
# The changes to a role that take access from whoever holds it.
_TAKES_ACCESS = frozenset(
    {"revoke", "revoke_grant_option", "revoke_role", "revoke_admin_option", "replace", "drop_role"}
)


class Refusal(AccessError):
    """`apply` won't start, for each of these reasons. Nothing has changed."""

    def __init__(self, *messages: str):
        super().__init__(" ".join(messages))
        self.messages = messages


@dataclass(frozen=True)
class Request:
    """What `access apply` was asked to do."""

    settings: ProjectSettings
    spec: AccessSpec
    reset_passwords: bool = False


@dataclass(frozen=True)
class Work:
    """What `apply` will do, in order, and what its statements are written from."""

    plan: Plan
    steps: tuple[Change, ...]  # the plan's lines, password resets and best-effort tries
    writing: Writing
    reader: Reader  # how the server is read, before and after the writes


@dataclass(frozen=True)
class Step:
    """How one change went: made, failed with the server's reason, or skipped and why."""

    change: Change
    failure: str | None = None
    skipped: str | None = None
    unkept: bool = False  # a replace not sent: it couldn't keep what is held

    @property
    def wrote(self) -> bool:
        """Whether a statement was sent for it."""
        return self.change.marker in "+-" and self.skipped is None


@dataclass
class Outcome:
    """Every step `run` took, in order."""

    steps: list[Step] = field(default_factory=list)
    # The roles each declared user or role holds as access.yaml says, through declared roles.
    holding: Mapping[Entity, frozenset[str]] = field(default_factory=dict)

    @property
    def counted(self) -> int:
        """The changes made: every `+` and `-` line but the best-effort tries."""
        return sum(1 for change in self.made if change.action != "best_effort")

    @property
    def wrote(self) -> bool:
        return any(step.wrote for step in self.steps)

    def skip_reason(self, change: Change) -> str | None:
        """Why a change must not run, or None (2.5).

        A user whose create or password reset failed gets nothing more.
        A revoke, replace, default-role change or drop waits for every earlier change: one
        that failed holds it back, and so does a replace that couldn't be sent. The reason
        names its own user or role, or a role it holds, when that is where it went wrong.
        Otherwise it names the change anywhere else, since a sibling role its holders rely on
        may be the one that failed.
        """
        if change.action in _DROPS:
            return self._drop_reason()
        login = self._failed_login(change.entity)
        if login:
            return f"its {login} failed, so this run gives it nothing more"
        if change.action not in _GATED:
            return None
        failed = {
            step.change.entity for step in self.failures if step.change.action in _BEFORE_REVOKES
        }
        unkept = {step.change.entity for step in self.steps if step.unkept}
        return (
            self._because(change.entity, failed, "failed")
            or self._because(change.entity, unkept, "couldn't be sent")
            or _elsewhere(failed, unkept)
        )

    def exit_code(self, after: Plan) -> int:
        """0 when the server now matches, else 1: a change failed, or something still differs."""
        return 1 if self.failures or after.pending or after.errors else 0

    @property
    def made(self) -> frozenset[Change]:
        return frozenset(step.change for step in self.steps if step.wrote and not step.failure)

    @property
    def failures(self) -> list[Step]:
        """The changes that failed. A best-effort try that fails is only a warning."""
        return [step for step in self.steps if step.failure and step.change.action != "best_effort"]

    @property
    def skipped(self) -> list[Step]:
        return [step for step in self.steps if step.skipped]

    def _failed_login(self, entity: Entity) -> str | None:
        """Which change that sets `entity`'s password failed, "create" or "password reset"."""
        for step in self.failures:
            if step.change.entity == entity and step.change.action in _SETS_PASSWORD:
                return _SETS_PASSWORD[step.change.action]
        return None

    def _drop_reason(self) -> str | None:
        """Why no drop runs yet: a change failed, or a replace couldn't be sent.

        Either can leave a revoke skipped, so a role about to be dropped may be the access a
        user or role keeps.
        """
        if self.failures:
            return "an earlier change failed"
        unkept = ", ".join(str(step.change.entity) for step in self.steps if step.unkept)
        return f"the replace of {unkept} couldn't be sent" if unkept else None

    def _because(self, entity: Entity, gating: set[Entity], verb: str) -> str | None:
        """Why `entity` waits, if `gating` names it or a role it holds as access.yaml says."""
        if entity in gating:
            return f"a change to {entity} {verb}, so it keeps the access it has"
        held = self.holding.get(entity, frozenset())
        roles = sorted(other.name for other in gating if other.kind == "role")
        roles = [role for role in roles if role in held]
        if roles:
            return f"{_changes_to(roles)}, which it holds, {verb}, so it keeps the access it has"
        return None


def prepare(reader: Reader, request: Request) -> Work:
    """Read, plan and check everything that can stop `apply`, before any write.

    Raises `Refusal`, or `AccessError` for a secret or the server, and nothing has changed.
    """
    _refuse_identities(reader.client, request)
    spec = request.spec
    live = reader.settled(wanted(spec), lambda state: build_plan(spec, state).missing_rows)
    plan = build_plan(spec, live)
    if plan.errors:
        raise Refusal(*plan.errors)
    _refuse_taking_from_the_login(reader.client, plan)
    created = tuple(change.entity.name for change in plan.changes if change.action == "create_user")
    resets = _resets(request, live, created)
    passwords = _passwords(request, created, resets)
    writing = Writing(spec, live, _cluster(request.settings, live), passwords)
    return Work(plan, _steps(plan, resets, _held_roles(spec)), writing, reader)


def run(client: Any, work: Work, report: Callable[[Step], None]) -> Outcome:
    """Make each change in order, reporting each step as it ends; go on past a failure."""
    outcome = Outcome(holding=_held_roles(work.writing.spec))
    for change in work.steps:
        skipped = outcome.skip_reason(change)
        steps = [Step(change, skipped=skipped)] if skipped else _make(client, work.writing, change)
        for step in steps:
            outcome.steps.append(step)
            report(step)
    return outcome


def replan(work: Work, outcome: Outcome) -> Plan:
    """Plan again, after reading until what `apply` made shows, or the time limit is up.

    Where every replica is read at once, a read is behind only while the replicas disagree
    (`LiveState.behind`). Where only the node connected to is read, a read is behind while a
    user or role that `apply` changed has a change pending, other than one `apply` failed at
    or skipped, which no read will show. The change pending needn't be one `apply` made: a
    replica that shows only the first statement of a replace leaves a grant option pending
    instead.
    """
    spec = work.writing.spec
    changed = {change.entity for change in outcome.made if change.action != "best_effort"}
    unmade = {step.change for step in outcome.steps if step.failure or step.skipped}
    live = work.reader.settled(
        wanted(spec),
        lambda state: any(
            change.entity in changed and change not in unmade
            for change in build_plan(spec, state).pending
        ),
    )
    return build_plan(spec, live)


def _refuse_identities(client: Any, request: Request) -> None:
    """`apply` never changes the admin's access, or that of the login it runs as (2.1, 2.5).

    The loader already refuses an access.yaml that declares `admin_user`. The server says
    who `apply` is really logged in as, and that login can't be declared either.
    """
    settings = request.settings
    if settings.migration_user == settings.admin_user:
        raise Refusal(
            f"The migration user {settings.migration_user} is config.yaml's admin_user, "
            "whose access ch-migrate never changes. Give the migration user a login of its own."
        )
    with server_errors():
        [row] = client.query(statement("apply", "current_user")).named_results()
    login = str(row["name"])
    if login in {user.name for user in request.spec.users}:
        raise Refusal(
            f"`apply` is logged in as {login}, which access.yaml declares. "
            "ch-migrate never changes the login it runs as."
        )


def _refuse_taking_from_the_login(client: Any, plan: Plan) -> None:
    """`apply` never takes access from a role the login it runs as holds (2.1).

    A revoke, replace or drop there could leave the admin without a right a later statement
    needs. The admin may hold a declared role, as it must hold one WITH ADMIN OPTION to grant
    it without ROLE ADMIN; only taking access from it is refused.
    """
    with server_errors():
        rows = client.query(statement("apply", "current_roles")).named_results()
    held = {str(row["name"]) for row in rows}
    taken = sorted(
        {
            change.entity.name
            for change in plan.changes
            if change.entity.kind == "role"
            and change.entity.name in held
            and change.action in _TAKES_ACCESS
        }
    )
    if taken:
        raise Refusal(
            f"access.yaml would revoke from, replace or drop {_roles_named(taken)}, which the "
            "login `apply` runs as holds. ch-migrate never takes access from the login it runs "
            "as: change access.yaml, or apply as an admin that doesn't hold it."
        )


def _resets(request: Request, live: LiveState, created: tuple[str, ...]) -> tuple[str, ...]:
    """With --reset-passwords: each declared user with a password, existing or created now.

    One this run creates is reset too, right after its create: if someone else makes the user
    between this run's read and its `CREATE USER IF NOT EXISTS`, the create keeps their
    password, and only the reset replaces it.
    """
    if not request.reset_passwords:
        return ()
    created_names = set(created)
    return tuple(
        user.name
        for user in _users(request.spec)
        if user.password is not None
        and (user.name in created_names or live.find("user", user.name) is not None)
    )


def _passwords(
    request: Request, created: tuple[str, ...], resets: tuple[str, ...]
) -> dict[str, str]:
    """Every password `apply` will set, read before its first write (2.5)."""
    spec = request.spec
    secrets = {user.name: user.password for user in _users(spec) if user.password is not None}
    secrets[spec.migration.user] = spec.migration.password
    return {name: read_secret(request.settings, secrets[name].name) for name in (*created, *resets)}


def _cluster(settings: ProjectSettings, live: LiveState) -> str | None:
    """The cluster to run every statement ON, or None where a create reaches every node."""
    cluster = settings.server.cluster
    if cluster is None or live.storage == REPLICATED_STORAGE:
        return None
    if live.storage is None:
        raise Refusal(
            f"config.yaml sets cluster {cluster} for {settings.environment}, but the admin "
            "can't read system.user_directories, or it lists no storage a create can go to, "
            "so `apply` can't tell whether users and roles need ON CLUSTER. "
            "Grant the admin SELECT ON system.user_directories."
        )
    return cluster


def _steps(
    plan: Plan, resets: tuple[str, ...], holding: Mapping[Entity, frozenset[str]]
) -> tuple[Change, ...]:
    """The plan's lines, each password reset and each best-effort try, in apply's order."""
    tries = [
        Change("best_effort", shortfall.entity, grant=shortfall.grant)
        for shortfall in plan.shortfalls
        if not shortfall.reason
    ]
    reset = [Change("reset_password", Entity("user", name)) for name in resets]
    steps = [*plan.changes, *reset, *tries]
    return tuple(
        sorted(steps, key=lambda change: (ACTION_ORDER[change.action], _depth(change, holding)))
    )


def _depth(change: Change, holding: Mapping[Entity, frozenset[str]]) -> int:
    """Where a replace runs among the replaces: the more roles it holds, the later (2.5).

    A user or role holds each role that a role it holds holds, and that role too, so a role's
    replace runs before the replace of any declared user or role that holds it.
    """
    if change.action != "replace":
        return 0
    return len(holding.get(change.entity, frozenset()))


def _held_roles(spec: AccessSpec) -> dict[Entity, frozenset[str]]:
    """The roles each declared user or role holds as access.yaml says, through declared roles.

    A role access.yaml doesn't declare counts, but what it holds isn't known, so the walk
    stops there.
    """
    declared = [entity for entity in declared_entities(spec) if not entity.absent]
    through = {entity.name: entity.roles for entity in declared if isinstance(entity, Role)}
    holding = {}
    for entity in declared:
        held: set[str] = set()
        left = list(entity.roles)
        while left:
            role = left.pop()
            if role not in held:
                held.add(role)
                left += through.get(role, ())
        kind = "user" if isinstance(entity, User) else "role"
        holding[Entity(kind, entity.name)] = frozenset(held)
    return holding


def _make(client: Any, writing: Writing, change: Change) -> list[Step]:
    """Send a change's statements in order; the first that fails ends it.

    A best-effort grant's statements are each a step of their own, and all are sent. A replace
    that can't keep what is held sends nothing, and is skipped.
    """
    try:
        writes = writes_for(change, writing)
    except Unkept as unkept:
        return [Step(change, skipped=str(unkept), unkept=True)]
    if change.action == "best_effort":
        return [
            Step(replace(change, grant=write.gives), failure=_send(client, writing, write))
            for write in writes
        ]
    for write in writes:
        failure = _send(client, writing, write)
        if failure is not None:
            return [Step(change, failure=failure)]
    return [Step(change)]


def _send(client: Any, writing: Writing, write: Write) -> str | None:
    """Send one statement: None when it ran, else the server's reason."""
    from clickhouse_connect.driver.exceptions import ClickHouseError

    # A statement with bound parameters, a password, goes by `raw_query`, which the client sends
    # form-encoded in the request body. `command` would put them in the URL.
    send = client.raw_query if write.parameters else client.command
    try:
        send(write.sql, parameters=dict(write.parameters) or None, settings=dict(write.settings))
    except ClickHouseError as error:
        return _reason(error, writing)
    return None


def _reason(error: Exception, writing: Writing) -> str:
    """The server's own message, with its error name, and no secret in it."""
    return hide_secrets(server_message(error), writing.passwords.values())


def _changes_to(roles: list[str]) -> str:
    """`a change to role a`, or `changes to roles a, b`."""
    if len(roles) == 1:
        return f"a change to role {roles[0]}"
    return "changes to roles " + ", ".join(roles)


def _users(spec: AccessSpec) -> list[User]:
    """The declared users that aren't `absent`: none in legacy mode."""
    return [
        declared
        for declared in declared_entities(spec)
        if isinstance(declared, User) and not declared.absent
    ]


def _elsewhere(failed: set[Entity], unkept: set[Entity]) -> str | None:
    """Why a change waits on a failure, or an unsent replace, of another user or role."""
    if failed:
        return f"an earlier change to {_entities(failed)} failed, so it keeps the access it has"
    if unkept:
        return f"the replace of {_entities(unkept)} couldn't be sent, so it keeps the access it has"
    return None


def _entities(entities: set[Entity]) -> str:
    return ", ".join(sorted(str(entity) for entity in entities))


def _roles_named(names: list[str]) -> str:
    """`role a`, or `roles a, b`."""
    return f"role {names[0]}" if len(names) == 1 else f"roles {', '.join(names)}"
