"""Own the upgrade process so SIGKILL cannot leave a child advancing revisions.

`ch-migrate up` runs Alembic in this process and reports in ch-migrate's own words,
as runner.py does for `down`: one line per migration, setup chatter dropped, and a
failure reduced to the migration, what went wrong and what to do next. Waiting
progress is drawn by a display the waiter receives through the Alembic config.
"""

from __future__ import annotations

import logging
import os
import re
import traceback
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from alembic import command
from alembic.config import Config

from ch_migrate import ui
from ch_migrate.runner import (
    _RUNNING,
    _SELF_DESCRIBING,
    _SETUP_CHATTER,
    _Progress,
    _report_success,
    _start_migration,
)
from ch_migrate.sql import clean_database_error
from ch_migrate.waiting.display import WaitDisplay
from ch_migrate.waiting.errors import (
    FailedMutation,
    MutationFailed,
    UnknownOutcome,
    WaitingError,
    WaitTimeout,
)

_LOCATION = re.compile(
    r"^(?P<path>migrations/sql/\S+) \(statement \d+ of \d+, line (?P<line>\d+)\): "
)


@dataclass(frozen=True)
class Upgrade:
    """One `ch-migrate up`: where, how far, how long to wait, and how much to show."""

    environment: str
    revision: str = "head"
    timeout: float | None = None
    verbose: bool = False


def run_upgrade(upgrade: Upgrade) -> int:
    """Apply migrations in this process, one reported line each; return the exit status."""
    progress, display = _Progress(), WaitDisplay()
    config = _alembic_config(upgrade, progress, display)
    # Project helpers intentionally read these during migration execution only.
    saved = {name: os.environ.get(name) for name in ("CH_DATABASE", "CH_CLUSTER")}
    try:
        with _restored_loggers("alembic", "ch_migrate"):
            command.upgrade(config, upgrade.revision)
    except KeyboardInterrupt:
        display.stop()
        _report_interrupt(upgrade.environment, progress)
        return 130
    except Exception as error:
        display.stop()
        _report_failure(upgrade, progress, error)
        return 1
    finally:
        _restore_environment(saved)
    _report_success(upgrade.environment, upgrade.revision, progress)
    return 0


class _MigrationLog(logging.Handler):
    """Turns Alembic's log records into ch-migrate's lines, as runner.py does for its output."""

    def __init__(self, progress: _Progress, display: WaitDisplay) -> None:
        super().__init__(logging.INFO)
        self.progress, self.display = progress, display

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        running = _RUNNING.match(message)
        if running:
            _start_migration(running, self.progress)
            self.display.start_migration(self.progress.current)
        elif message.startswith(_SETUP_CHATTER):
            return
        elif record.levelno >= logging.WARNING:
            ui.warn(message)
        else:
            ui.detail(message)


def _alembic_config(upgrade: Upgrade, progress: _Progress, display: WaitDisplay) -> Config:
    """The project's Alembic config, carrying what env.py needs from this invocation."""
    config = Config(str(Path.cwd() / "alembic.ini"))
    config.attributes.update(
        ch_migrate_environment=upgrade.environment,
        ch_migrate_command="upgrade",
        ch_migrate_timeout=upgrade.timeout,
        ch_migrate_display=display,
        ch_migrate_log_handler=_MigrationLog(progress, display),
    )
    return config


def _report_interrupt(environment: str, progress: _Progress) -> None:
    ui.error(f"Interrupted{' during ' + progress.current if progress.current else ''}")
    ui.hint(
        "Work ClickHouse already started keeps running, and the migration is not recorded as "
        f"applied. Run `ch-migrate up {environment}` to pick up where it stopped; it won't "
        "reissue a statement it already sent.",
        stderr=True,
    )


def _report_failure(upgrade: Upgrade, progress: _Progress, error: Exception) -> None:
    where = progress.current or "Upgrade"
    waiting = _waiting_error(error)
    if waiting is not None and (location := _LOCATION.match(str(error))):
        where = f"{where} ({location['path']}:{location['line']})"
    if isinstance(waiting, MutationFailed):
        _report_mutation_failure(upgrade.environment, where, waiting.failure)
    elif isinstance(waiting, WaitTimeout):
        _report_timeout(upgrade.environment, where, waiting)
    elif isinstance(waiting, UnknownOutcome):
        _report_unknown_outcome(where, waiting)
    else:
        _report_statement_failure(upgrade, where, error)


def _report_mutation_failure(environment: str, where: str, failure: FailedMutation) -> None:
    on_host = "" if failure.host == "shared" else f" on {failure.host}"
    if failure.foreign:
        ui.error(
            f"{where} is blocked: foreign mutation {failure.mutation} on {failure.table} "
            f"failed{on_host}"
        )
    else:
        ui.error(f"{where} failed: mutation {failure.mutation} on {failure.table} failed{on_host}")
    ui.detail(clean_database_error(failure.reason), stderr=True)
    ui.hint(
        "It was not recorded as applied. ClickHouse keeps retrying a failed mutation, and later "
        f"mutations on {failure.table} wait behind it.",
        stderr=True,
    )
    ui.hint(
        "If the cause is temporary, such as memory or disk, fix it and run "
        f"`ch-migrate up {environment}` to wait for the retry.",
        stderr=True,
    )
    ui.hint(
        f"If the statement itself is wrong, an operator stops it{on_host} with {failure.kill}, "
        "then reconciles the journal as the README's Unknown outcome section describes.",
        stderr=True,
    )


def _report_timeout(environment: str, where: str, timeout: WaitTimeout) -> None:
    ui.error(f"Timed out after {timeout.elapsed:.1f}s waiting on {where}")
    ui.detail(f"Pending: {timeout.pending}", stderr=True)
    ui.hint(
        "ClickHouse keeps doing the work, and the migration is not recorded as applied. "
        f"Run `ch-migrate up {environment}` again to keep waiting; it won't reissue the statement.",
        stderr=True,
    )


def _report_unknown_outcome(where: str, unknown: UnknownOutcome) -> None:
    ui.error(f"{where}: outcome unknown, so the statement was not reissued")
    for line in str(unknown).strip().splitlines():
        ui.detail(line, stderr=True)
    ui.hint("Follow the README's Unknown outcome steps before you run `up` again.", stderr=True)


def _report_statement_failure(upgrade: Upgrade, where: str, error: Exception) -> None:
    ui.error(f"{where} failed")
    ui.detail(_reason(error), stderr=True)
    ui.hint(
        "It was not recorded as applied. Anything it ran before the failure stays in place, "
        f"so make the SQL safe to re-run, then run `ch-migrate up {upgrade.environment}` again.",
        stderr=True,
    )
    if upgrade.verbose:
        traceback.print_exception(error)
    else:
        ui.hint("Add `--verbose` to see the full traceback.", stderr=True)


def _reason(error: Exception) -> str:
    lines = str(error).strip().splitlines()
    message = clean_database_error(lines[0]) if lines else ""
    kind = type(error).__name__
    if not message:
        return kind
    return message if kind in _SELF_DESCRIBING else f"{kind}: {message}"


def _waiting_error(error: BaseException) -> WaitingError | None:
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        if isinstance(current, WaitingError):
            return current
        seen.add(id(current))
        current = getattr(current, "orig", None) or current.__cause__ or current.__context__
    return None


@contextmanager
def _restored_loggers(*names: str):
    saved = [(logging.getLogger(n), logging.getLogger(n).handlers[:]) for n in names]
    states = [(logger.propagate, logger.level) for logger, _ in saved]
    try:
        yield
    finally:
        for (logger, handlers), (propagate, level) in zip(saved, states):
            logger.handlers, logger.propagate = handlers, propagate
            logger.setLevel(level)


def _restore_environment(saved: dict[str, str | None]) -> None:
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
