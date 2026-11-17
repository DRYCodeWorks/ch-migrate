"""The access commands and their CLI error boundaries (design 2.5).

Validate is offline; show and plan use the admin; check uses the migration login.
Apply exits 2 only before any write. Once a write may have run, failures exit 1.
Command help defines outputs and exit codes; live.Reader owns replica settlement.
"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
from typing import Any, Callable, NoReturn

import click

from ch_migrate import ui
from ch_migrate.access import applier
from ch_migrate.access.check import check_migration
from ch_migrate.access.loader import ACCESS_FILE, Validation, validate_access
from ch_migrate.access.model import AccessSpec
from ch_migrate.access.planner import Plan, build_plan, wanted
from ch_migrate.access.render import render_sql, users_that_must_exist
from ch_migrate.access.report import (
    print_change,
    reader,
    report_check,
    report_node,
    report_step,
)
from ch_migrate.access.server import AccessError, connect_admin, connect_migration, hide_secrets
from ch_migrate.access.settings import ProjectSettings, SettingsError, load_settings
from ch_migrate.access.show import Showing, show_access

# Exit 2: an invalid file or config.yaml, or an error in a live command (2.5).
INVALID = 2


@click.group()
def access() -> None:
    """Declare users, roles and grants in access.yaml."""


@access.command()
@click.argument("environment")
@click.option(
    "--sql", "show_sql", is_flag=True, help="Also print the SQL a DBA can review and run."
)
def validate(environment: str, show_sql: bool) -> None:
    """Check access.yaml and the built-in profiles for ENVIRONMENT, offline.

    Needs no server and no secrets. Without access.yaml, checks legacy mode: the grants
    `bootstrap` gave in 0.8. Exits 0 when valid and 2 when not, listing every error.
    """
    settings = _settings_or_exit(environment)
    result = validate_access(settings, Path.cwd())
    for warning in result.warnings:
        ui.warn(str(warning))
    if result.spec is None:
        _report_errors(result)
    if show_sql:
        click.echo(_sql_text(result.spec))
    _report_valid(result.spec, settings, stderr=show_sql)


@access.command()
@click.argument("environment")
@click.argument("names", nargs=-1)
def show(environment: str, names: tuple[str, ...]) -> None:
    """Print what the server holds, in access.yaml form, for ENVIRONMENT.

    Covers the users and roles access.yaml declares, or the NAMES given. It is where
    adopting existing users and roles starts: write it to another file and copy what you
    keep into access.yaml. Redirected into access.yaml itself, the shell empties the file
    first. Logs in as the admin, writes nothing and
    never shows a password. Exits 0, or 2 on an error.
    """
    _exit_2_on_failure(lambda: _show(environment, names))


@access.command()
@click.argument("environment")
def plan(environment: str) -> None:
    """Show how the server differs from access.yaml for ENVIRONMENT, changing nothing.

    One line per change: `+` create or grant, `-` revoke or drop, `~` adopt a user or role
    that already exists, `!` a grant the migration role holds beyond its profile. Logs in as
    the admin and reads no other secret. Exits 0 in sync, 1 with changes pending, 2 on an
    error. `!` lines and best-effort warnings leave the exit code alone.
    """
    _exit_2_on_failure(lambda: _plan(environment))


@access.command()
@click.argument("environment")
@click.option(
    "--reset-passwords",
    is_flag=True,
    help="Set every declared user's password again, from its secret.",
)
def apply(environment: str, reset_passwords: bool) -> None:
    """Make the server match access.yaml for ENVIRONMENT.

    Logs in as the admin and makes what `plan` lists, in order: creates, memberships,
    grants, default roles, revokes, then drops. A user's or role's revokes run only once
    everything granted to it has landed, and drops only if nothing failed. Best-effort
    grants land as far as the admin can give them, and the rest is a warning. A password is
    set when its user is created, or again with --reset-passwords. Exits 0 when the server
    matches, 1 when a change failed (each one named), and 2 on an error before any change.
    """
    _exit_2_on_failure(lambda: _apply(environment, reset_passwords))


@access.command()
@click.argument("environment")
def check(environment: str) -> None:
    """Check that the migration user holds the migration role's grants for ENVIRONMENT.

    For servers where ch-migrate has no admin login: it logs in as the migration user and
    sends one CHECK GRANT for each grant of the role's profile and `migration_role.grants`,
    without grant option, which CHECK GRANT can't check. Exits 0 when every required grant
    is held, 1 when one is missing (each one named), and 2 on an error. A missing
    best-effort grant is a warning. Needs ClickHouse 25.8 or later.
    """
    _exit_2_on_failure(lambda: _check(environment))


def _exit_2_on_failure(command: Callable[[], None]) -> None:
    """Run a live command so that an error that stops it prints and exits 2, never 1."""
    try:
        command()
        return
    except (click.exceptions.Exit, click.exceptions.Abort):
        raise
    except click.ClickException as error:
        error.show()
    except AccessError as error:
        ui.error(hide_secrets(str(error)))
    except Exception as error:
        ui.error(hide_secrets(f"{type(error).__name__}: {error}"))
    raise click.exceptions.Exit(INVALID)


def _show(environment: str, names: tuple[str, ...]) -> None:
    settings, spec = _declaration(environment)
    showing = Showing(settings, spec, names)
    showing.check()
    with closing(connect_admin(settings)) as client:
        live = reader(client, settings).settled(showing.wanted, showing.unsettled)
    shown = show_access(showing, live)
    for warning in shown.warnings:
        ui.warn(warning)
    click.echo(shown.text, nl=False)


def _plan(environment: str) -> None:
    settings, spec = _declaration(environment)
    with closing(connect_admin(settings)) as client:
        live = reader(client, settings).settled(
            wanted(spec), lambda state: build_plan(spec, state).missing_rows
        )
        report_node(client, settings, live)
    result = build_plan(spec, live)
    _report_plan(result, spec)
    raise click.exceptions.Exit(result.exit_code)


def _apply(environment: str, reset_passwords: bool) -> None:
    settings, spec = _declaration(environment)
    with closing(connect_admin(settings)) as client:
        request = applier.Request(settings, spec, reset_passwords)
        try:
            work = applier.prepare(reader(client, settings), request)
        except applier.Refusal as refusal:
            for message in refusal.messages:
                ui.error(message)
            raise click.exceptions.Exit(INVALID) from None
        report_node(client, settings, work.writing.live)
        code = _applied(client, work)
    raise click.exceptions.Exit(code)


def _check(environment: str) -> None:
    settings, spec = _declaration(environment)
    with closing(connect_migration(settings)) as client:
        checked = check_migration(client, spec.migration)
    report_check(checked, best_effort=True)
    raise click.exceptions.Exit(checked.exit_code)


def _applied(client: Any, work: applier.Work) -> int:
    """Make the changes, plan again and report. Once a write may have run, nothing exits 2."""
    try:
        outcome = applier.run(client, work, report_step)
        after = applier.replan(work, outcome) if outcome.wrote else work.plan
    except Exception as error:  # noqa: BLE001 - after a write, every error is exit 1
        reason = (
            str(error) if isinstance(error, AccessError) else f"{type(error).__name__}: {error}"
        )
        ui.error(f"`apply` stopped part-way: {hide_secrets(reason)}")
        ui.hint("`ch-migrate access plan` shows what still differs.", stderr=True)
        return 1
    except KeyboardInterrupt:
        # Ctrl-C isn't an Exception, but a write may have run all the same. 130, as
        # `up` exits.
        ui.error("`apply` stopped part-way: interrupted.")
        ui.hint("`ch-migrate access plan` shows what still differs.", stderr=True)
        return 130
    _report_applied(work, outcome, after)
    return outcome.exit_code(after)


def _declaration(environment: str) -> tuple[ProjectSettings, AccessSpec]:
    """config.yaml's settings and the valid declaration, or exit 2 listing every error."""
    settings = _settings_or_exit(environment)
    result = validate_access(settings, Path.cwd())
    if result.spec is None:
        _report_errors(result)
    return settings, result.spec


def _settings_or_exit(environment: str) -> ProjectSettings:
    try:
        return load_settings(environment, Path.cwd() / "config.yaml")
    except SettingsError as error:
        ui.error(str(error))
        raise click.exceptions.Exit(INVALID) from None


def _report_errors(result: Validation) -> NoReturn:
    for problem in result.errors:
        ui.error(str(problem))
    count = len(result.errors)
    ui.error(f"Invalid: {count} error{'' if count == 1 else 's'}.")
    raise click.exceptions.Exit(INVALID)


def _sql_text(spec: AccessSpec) -> str:
    source = "legacy mode, as 0.8's `bootstrap`" if spec.legacy else ACCESS_FILE
    header = [
        f"-- Access for {spec.environment}, from {source}.",
        "-- Replace each <secret:NAME> with that secret's value as a quoted string, such as",
        "-- 'value', before running it. Left in place, it fails to parse.",
    ]
    existing = ", ".join(users_that_must_exist(spec))
    if existing:
        header.append(
            f"-- Users with no password aren't created here and must already exist: {existing}."
        )
    return "\n".join([*header, *(f"{sql};" for sql in render_sql(spec))])


def _report_valid(spec: AccessSpec, settings: ProjectSettings, *, stderr: bool) -> None:
    migration = f"the migration role {settings.migration_role} holds the {spec.migration.profile.name} profile"
    if spec.legacy:
        ui.success(f"No {ACCESS_FILE}: legacy mode, where {migration}.", stderr=stderr)
        return
    declared = f"{_count(spec.roles, 'role')} and {_count(spec.users, 'user')} declared"
    ui.success(
        f"{ACCESS_FILE} is valid for {spec.environment}: {declared}; {migration}.", stderr=stderr
    )


def _report_plan(result: Plan, spec: AccessSpec) -> None:
    """The plan's lines on stdout; shortfalls and errors on stderr."""
    if result.errors:
        for message in result.errors:
            ui.error(message)
        return
    for change in result.changes:
        print_change(change)
    for shortfall in result.shortfalls:
        ui.warn(str(shortfall))
    count = len(result.pending)
    if count:
        ui.step(f"{_changes(count)} pending for {spec.environment} ({_source(spec)}).")
    else:
        ui.success(f"In sync for {spec.environment} ({_source(spec)}): nothing to change.")


def _report_applied(work: applier.Work, outcome: applier.Outcome, after: Plan) -> None:
    """Best-effort warnings, then in sync, or what still differs and what failed."""
    spec = work.writing.spec
    for shortfall in after.shortfalls:
        ui.warn(str(shortfall))
    for message in after.errors:
        ui.error(message)
    where = f"{spec.environment} ({_source(spec)})"
    applied = f"{_changes(outcome.counted)} applied"
    if not outcome.exit_code(after):
        ui.success(f"In sync for {where}: {applied}.")
        return
    if after.pending:
        ui.step(f"Still pending for {where}:")
        for change in after.pending:
            print_change(change)
    failed, skipped = len(outcome.failures), len(outcome.skipped)
    ui.error(f"Not in sync for {where}: {applied}, {failed} failed, {skipped} skipped.")


def _count(entities: tuple[object, ...], noun: str) -> str:
    return f"{len(entities)} {noun}" if len(entities) == 1 else f"{len(entities)} {noun}s"


def _source(spec: AccessSpec) -> str:
    return "legacy mode, the migration role and user only" if spec.legacy else ACCESS_FILE


def _changes(count: int) -> str:
    return "1 change" if count == 1 else f"{count} changes"
