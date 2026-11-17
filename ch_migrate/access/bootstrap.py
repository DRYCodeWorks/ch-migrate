"""Bootstrap the database and migration identity, then check as the migration user.

With access.yaml, use the applier for the additive migration identity only. The remaining
declaration belongs to access apply. Legacy mode retains 0.8's per-node statements and
CURRENT GRANTS, continues past failures, and skips unsupported read-back on old servers.
Refusals and secrets precede every write; later errors return 1 (design 2.5).
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from ch_migrate import ui
from ch_migrate.access import applier
from ch_migrate.access.check import Checked, check_migration
from ch_migrate.access.legacy import OPTIONAL_PASSWORDS, legacy_spec
from ch_migrate.access.live import LiveState
from ch_migrate.access.loader import ACCESS_FILE, validate_access
from ch_migrate.access.model import AccessSpec, Grant, Role, User
from ch_migrate.access.planner import Change, Entity, Plan, build_plan, wanted
from ch_migrate.access.privileges import bundled_tree
from ch_migrate.access.profiles import load_profiles
from ch_migrate.access.render import PasswordText, render_bootstrap_sql
from ch_migrate.access.report import (
    print_change,
    reader,
    report_check,
    report_node,
    report_step,
)
from ch_migrate.access.server import (
    AccessError,
    Login,
    hide_secrets,
    open_client,
    optional_secret,
    read_secret,
    require_supported_version,
    secret_is_set,
)
from ch_migrate.access.settings import ProjectSettings
from ch_migrate.access.writes import Writing

# The changes that create a user or role, which a read after the write waits for.
_CREATES = frozenset({"create_role", "create_user"})


@dataclass(frozen=True)
class Bootstrap:
    """What `ch-migrate bootstrap ENV` was asked to do."""

    settings: ProjectSettings
    root: Path  # the project directory, which holds access.yaml or doesn't
    verbose: bool = False  # print each statement as it is sent


def run(request: Bootstrap) -> int:
    """Bootstrap, then check. 0 when it is all in place, else 1, once each failure is named.

    Raises before the first write, with nothing changed.
    """
    spec = declaration(request)
    settings = request.settings
    if spec.legacy:
        spec = _without_unset_optional_users(request, spec, _readable)
        passwords = _legacy_passwords(settings, spec)
    else:
        read_secret(settings, spec.migration.password.name)  # `check` logs in with it
    with closing(open_client(settings, _admin(settings))) as admin:
        # Every bootstrap needs 25.8, legacy too: 0.8's already failed on 24.8, and the
        # read-back and CHECK GRANT need it.
        require_supported_version(str(admin.server_version))
        if spec.legacy:
            run = _Run(request, spec, admin)
            ui.step(_heading(run))
            return _guarded(lambda: _legacy_writes(run, passwords))
        run = _Run(request, _bootstrapped(spec), admin)
        work = _declared_work(run)
        ui.step(_heading(run))
        report_node(admin, settings, work.writing.live)
        return _guarded(lambda: _declared_writes(run, work))


def dry_run_sql(request: Bootstrap) -> str:
    """What `bootstrap` would run, with secret placeholders: no server and no secret."""
    spec = declaration(request)
    if spec.legacy:
        spec = _without_unset_optional_users(request, spec, secret_is_set)
    settings = request.settings
    statements = render_bootstrap_sql(_bootstrapped(spec), settings.database)
    return "\n".join([*_dry_run_header(settings, spec), *(f"{sql};" for sql in statements)])


def declaration(request: Bootstrap) -> AccessSpec:
    """access.yaml for the environment, or legacy mode without one; a `Refusal` if invalid."""
    result = validate_access(request.settings, request.root)
    if result.spec is None:
        count = len(result.errors)
        summary = f"Invalid: {count} error{'' if count == 1 else 's'}."
        raise applier.Refusal(*(str(problem) for problem in result.errors), summary)
    return result.spec


def legacy_bootstrap_sql(settings: ProjectSettings, password: PasswordText) -> str:
    """0.8's bootstrap SQL, from the `legacy` profile, for `build_bootstrap_sql`.

    The same statements as 0.8's, with the roles and users created before any grant.
    """
    tree = bundled_tree()
    profiles, _ = load_profiles(settings.placeholders(), tree)
    spec, _ = legacy_spec(settings, profiles["legacy"], tree)
    statements = render_bootstrap_sql(spec, settings.database, password)
    return "\n".join(f"{sql};" for sql in statements)


@dataclass(frozen=True)
class _Run:
    """One bootstrap under way: what was asked, what it makes, and the admin's client."""

    request: Bootstrap
    spec: AccessSpec  # the migration identity, and in legacy mode its extra roles and users
    admin: Any

    @property
    def settings(self) -> ProjectSettings:
        return self.request.settings


@dataclass(frozen=True)
class _Echo:
    """The admin's client, printing each statement as it is sent: `bootstrap --verbose`.

    A password is a bound parameter sent in the request body, so it is never part of what is
    printed.
    """

    client: Any

    def command(self, sql: str, parameters: Any = None, settings: Any = None) -> Any:
        ui.detail(f"{sql};")
        return self.client.command(sql, parameters=parameters, settings=settings)

    def raw_query(self, sql: str, parameters: Any = None, settings: Any = None) -> Any:
        ui.detail(f"{sql};")
        return self.client.raw_query(sql, parameters=parameters, settings=settings)


@dataclass(frozen=True)
class _Ending:
    """How a bootstrap's writes and check went, for its closing line."""

    failed: int  # changes that failed; a refused best-effort grant is only a warning
    checked: Checked | None  # None when the check couldn't run
    plan_errors: int = 0  # what planning again found that the declaration can't have
    pending: int = 0  # changes planning again still found, as `apply` reports them


def _without_unset_optional_users(
    request: Bootstrap, spec: AccessSpec, is_set: Callable[[ProjectSettings, str], bool]
) -> AccessSpec:
    """Legacy mode without each optional user whose password isn't set, as 0.8 skipped it.

    `mcp_user_name` or `dict_reader_name` with no password set made nothing in 0.8; reading the
    secret as required stopped 0.9's bootstrap instead. The real run
    reads the secret to tell; `--dry-run` reads none, so it asks only whether one is set.
    """
    settings = request.settings
    unset = {
        key: secret
        for key, secret in OPTIONAL_PASSWORDS.items()
        if getattr(settings, key) and not is_set(settings, secret)
    }
    for key, secret in unset.items():
        ui.warn(
            f"{key} names {getattr(settings, key)}, but {secret} isn't set, so `bootstrap` "
            "skips that user and its role, as 0.8 did."
        )
    if not unset:
        return spec
    trimmed = replace(
        settings,
        mcp_user_name=None if "mcp_user_name" in unset else settings.mcp_user_name,
        dict_reader_name=None if "dict_reader_name" in unset else settings.dict_reader_name,
    )
    return declaration(replace(request, settings=trimmed))


def _readable(settings: ProjectSettings, name: str) -> bool:
    return optional_secret(settings, name) is not None


def _legacy_passwords(settings: ProjectSettings, spec: AccessSpec) -> dict[str, str]:
    """Every password legacy mode sets, by user, read before the first write."""
    migration = spec.migration
    secrets = {migration.user: migration.password}
    secrets.update({user.name: user.password for user in spec.users if user.password})
    return {name: read_secret(settings, secret.name) for name, secret in secrets.items()}


def _guarded(write: Callable[[], int]) -> int:
    """Run the writes and the check; once a write may have run, an error is exit 1, never 2."""
    try:
        return write()
    except Exception as error:  # noqa: BLE001 - after a write, every error is exit 1
        reason = (
            str(error) if isinstance(error, AccessError) else f"{type(error).__name__}: {error}"
        )
        ui.error(f"`bootstrap` stopped part-way: {hide_secrets(reason)}")
        return 1
    except KeyboardInterrupt:
        # Ctrl-C isn't an Exception, but a write may have run all the same. 130, as
        # `up` exits.
        ui.error("`bootstrap` stopped part-way: interrupted. Run it again: it only adds.")
        return 130


def _legacy_writes(run: _Run, passwords: dict[str, str]) -> int:
    """Send every statement, then read back and check (Decision 6)."""
    settings = run.settings
    writing = Writing(run.spec, LiveState(bundled_tree()), None, passwords)  # Decision 13
    work = applier.Work(
        Plan(), _legacy_steps(run.spec, settings.database), writing, reader(run.admin, settings)
    )
    outcome = applier.run(_client(run), work, _report_trouble)
    _warn_shortfalls(run, work, outcome)
    return _finish(run, _Ending(len(outcome.failures), _check(run)))


def _declared_work(run: _Run) -> applier.Work:
    """`apply`'s work for the migration identity, with the database first."""
    settings = run.settings
    work = applier.prepare(reader(run.admin, settings), applier.Request(settings, run.spec))
    database = Change("create_database", Entity("database", settings.database))
    return replace(work, steps=(database, *work.steps))


def _declared_writes(run: _Run, work: applier.Work) -> int:
    outcome = applier.run(_client(run), work, report_step)
    after = applier.replan(work, outcome)
    for shortfall in after.shortfalls:
        ui.warn(str(shortfall))
    for message in after.errors:
        ui.error(message)
    # A replica still behind at the deadline leaves a change pending even when the check, on
    # the node connected to, passes; `apply` reports the same.
    if after.pending:
        ui.step(f"Still pending for {run.settings.environment}:")
        for change in after.pending:
            print_change(change)
    ending = _Ending(len(outcome.failures), _check(run), len(after.errors), len(after.pending))
    return _finish(run, ending)


def _legacy_steps(spec: AccessSpec, database: str) -> tuple[Change, ...]:
    """0.8's statements, the database, roles and users first. Legacy mode's read-only and
    dictionary roles and users follow the migration identity's own."""
    migration = spec.migration
    holders: list[Role | User] = [*spec.roles, *spec.users]
    role, user = Entity("role", migration.role), Entity("user", migration.user)
    return (
        Change("create_database", Entity("database", database)),
        *(Change("create_role", entity) for entity in (role, *map(_entity, spec.roles))),
        *(Change("create_user", entity) for entity in (user, *map(_entity, spec.users))),
        Change("grant_role", user, roles=(migration.role,)),
        *(Change("grant_role", _entity(h), roles=(name,)) for h in holders for name in h.roles),
        *(_granting(role, grant) for grant in migration.all_grants),
        *(_granting(_entity(h), grant) for h in holders for grant in h.grants),
    )


def _warn_shortfalls(run: _Run, work: applier.Work, outcome: applier.Outcome) -> None:
    """Read back, as the admin, what landed; each best-effort grant that fell short warns."""
    spec = run.spec
    made = [change.entity for change in outcome.made if change.action in _CREATES]
    try:
        live = work.reader.settled(
            wanted(spec),
            lambda state: any(state.find(entity.kind, entity.name) is None for entity in made),
        )
    except AccessError as error:
        ui.warn(
            f"Can't read back what landed, as {run.settings.admin_user}: "
            f"{hide_secrets(str(error))}"
        )
        return
    for shortfall in build_plan(spec, live).shortfalls:
        ui.warn(str(shortfall))


def _check(run: _Run) -> Checked | None:
    """`access check` as the migration user; None, once it is named, for an error."""
    settings, migration = run.settings, run.spec.migration
    try:
        with closing(
            open_client(settings, Login(migration.user, migration.password.name))
        ) as client:
            checked = check_migration(client, migration)
    except AccessError as error:
        ui.error(
            f"Can't check the grants of user {migration.user}, logged in as it: "
            f"{hide_secrets(str(error))}"
        )
        return None
    report_check(checked, best_effort=False)
    return checked


def _finish(run: _Run, ending: _Ending) -> int:
    """The last line: complete, or each kind of problem counted. Returns the exit code."""
    problems = _problems(run, ending)
    environment = run.settings.environment
    if problems:
        ui.error(f"Bootstrap of {environment} isn't complete: {'; '.join(problems)}.")
        return 1
    ui.success(f"Bootstrap complete for {environment}.")
    ui.hint(
        f"Next: `ch-migrate new {environment} NAME --table TABLE`, or `ch-migrate up {environment}`."
    )
    return 0


def _problems(run: _Run, ending: _Ending) -> list[str]:
    """Failed changes, the plan's errors and the check's findings, each counted on its own."""
    problems = [f"{_count(ending.failed, 'change')} failed"] if ending.failed else []
    if ending.plan_errors:
        problems.append(f"planning again found {_count(ending.plan_errors, 'error')}")
    if ending.pending:
        problems.append(f"{_count(ending.pending, 'change')} still pending")
    checked = ending.checked
    if checked is None:
        problems.append("its grants couldn't be checked")
    elif checked is not None and checked.missing:
        lacking = _count(len(checked.missing), "required grant")
        problems.append(f"user {checked.user} lacks {lacking}")
    return problems


def _heading(run: _Run) -> str:
    settings, spec = run.settings, run.spec
    where = f"database {settings.database} on {settings.server.host}, as {settings.admin_user}"
    if spec.legacy:
        return f"Bootstrapping {settings.environment}: {where}, in legacy mode (no {ACCESS_FILE})"
    profile = spec.migration.profile.name
    return f"Bootstrapping {settings.environment}: {where}, with the {profile} profile"


def _dry_run_header(settings: ProjectSettings, spec: AccessSpec) -> list[str]:
    if spec.legacy:
        source = "legacy mode, as 0.8's `bootstrap`"
    else:
        source = f"{ACCESS_FILE}: the database and the migration role and user only"
    header = [
        f"-- Bootstrap for {settings.environment}, from {source}.",
        "-- Replace each <secret:NAME> with that secret's value as a quoted string, such as",
        "-- 'value', before running it. Left in place, it fails to parse.",
    ]
    cluster = settings.server.cluster
    if cluster and not spec.legacy:
        header += [
            f"-- With cluster {cluster} set, bootstrap runs each statement ON CLUSTER where users",
            "-- and roles are stored on each node's own disk, and each best-effort privilege as",
            "-- a grant of its own. Offline, the dry run can't tell, so it shows them without.",
        ]
    return header


def _admin(settings: ProjectSettings) -> Login:
    return Login(settings.admin_user, "admin_password")


def _bootstrapped(spec: AccessSpec) -> AccessSpec:
    """What `bootstrap` makes: with an access.yaml, only the migration identity (2.5)."""
    return spec if spec.legacy else replace(spec, roles=(), users=())


def _client(run: _Run) -> Any:
    return _Echo(run.admin) if run.request.verbose else run.admin


def _report_trouble(step: applier.Step) -> None:
    """Legacy mode sends every statement on every run, so it names only what went wrong."""
    if step.failure or step.skipped:
        report_step(step)


def _entity(holder: Role | User) -> Entity:
    return Entity("user" if isinstance(holder, User) else "role", holder.name)


def _granting(entity: Entity, grant: Grant) -> Change:
    return Change("best_effort" if grant.best_effort else "grant", entity, grant=grant)


def _count(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"
