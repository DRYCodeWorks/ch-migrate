"""Run Alembic for an environment and report what happened in ch-migrate's own words.

Alembic runs in a subprocess so each project's env.py and revision files load
exactly as `alembic` would load them. Its log lines are translated: each
"Running upgrade" becomes one line naming the migration, setup chatter is dropped,
and a failure is reduced to the migration, the file and the database error. The
full traceback is printed only with --verbose.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import click

from ch_migrate import ui
from ch_migrate.config import get_env_config
from ch_migrate.sql import clean_database_error

_LOG_LINE = re.compile(r"^(?P<level>[A-Z]+)\s+\[(?P<logger>[^\]]+)\]\s?(?P<message>.*)$")
_RUNNING = re.compile(
    r"^Running (?P<direction>upgrade|downgrade) (?P<source>\S*) -> (?P<target>\S*), ?(?P<name>.*)$"
)
_SETUP_CHATTER = ("Context impl ", "Will assume ")
_EXCEPTION_LINE = re.compile(r"^(?:[\w.]+\.)?(?P<kind>\w+): (?P<message>.*)$", re.DOTALL)
_SELF_DESCRIBING = {"SqlStatementError", "IrreversibleMigration"}


def run_migrations(environment: str, args: list[str], *, verbose: bool) -> int:
    """Run `alembic upgrade|downgrade TARGET`, reporting one line per migration.

    Returns Alembic's exit status.
    """
    progress = _Progress()
    command = [sys.executable, "-m", "alembic", *args]
    with subprocess.Popen(
        command,
        env=alembic_env(environment),
        cwd=Path.cwd(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    ) as process:
        assert process.stdout is not None
        for line in process.stdout:
            _handle_line(line.rstrip("\n"), progress)
        status = process.wait()
    if status == 0:
        _report_success(environment, args[-1], progress)
    else:
        _report_failure(environment, progress, verbose)
    return status


def run_alembic(environment: str, args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run an Alembic command silently and return its captured output."""
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        env=alembic_env(environment),
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
    )


def alembic_failure(result: subprocess.CompletedProcess[str]) -> str:
    """The one line worth showing from a failed Alembic command."""
    lines = (result.stderr or result.stdout).splitlines()
    for line in lines:
        if line.strip().startswith("FAILED:"):
            return line.strip()[len("FAILED:") :].strip()
    return _exception_message(lines) or f"Alembic exited with status {result.returncode}"


def alembic_env(environment: str) -> dict[str, str]:
    """Process environment for an Alembic subprocess targeting this environment."""
    try:
        env_config = get_env_config(environment, Path.cwd() / "config.yaml")
    except Exception as exc:
        ui.fail(f"Could not load config: {exc}")
    env = os.environ.copy()
    env.update(
        CH_ENVIRONMENT=environment,
        CH_DATABASE=env_config["database"],
        CH_HOST=env_config["host"],
        CH_PORT=str(env_config.get("port", 8443)),
        CH_USER=env_config.get("migration_user") or env_config.get("user", ""),
        CH_PASSWORD=env_config.get("password", ""),
        CH_SECURE="1" if env_config.get("secure", True) else "0",
        # Keep migration print() output in order with Alembic's log lines.
        PYTHONUNBUFFERED="1",
    )
    return env


@dataclass
class _Progress:
    direction: str = "upgrade"
    current: str = ""
    count: int = 0
    failure: str = ""
    traceback: list[str] = field(default_factory=list)


def _handle_line(line: str, progress: _Progress) -> None:
    if progress.traceback or line.startswith("Traceback (most recent call last):"):
        progress.traceback.append(line)
        return
    if line.strip().startswith("FAILED:"):
        progress.failure = line.strip()[len("FAILED:") :].strip()
        return
    logged = _LOG_LINE.match(line)
    if logged is None:
        click.echo(line)  # the migration's own output, shown as written
        return
    message = logged["message"]
    running = _RUNNING.match(message)
    if running:
        _start_migration(running, progress)
    elif message.startswith(_SETUP_CHATTER):
        return
    elif logged["level"] in ("WARNING", "WARN", "ERROR", "CRITICAL"):
        ui.warn(message)
    else:
        ui.detail(message)


def _start_migration(running: re.Match[str], progress: _Progress) -> None:
    progress.direction = running["direction"]
    revision = running["target"] if progress.direction == "upgrade" else running["source"]
    progress.current = f"{revision[:8]}  {running['name']}".rstrip()
    progress.count += 1
    verb = "Applying" if progress.direction == "upgrade" else "Reverting"
    ui.step(f"{verb} {progress.current}")


def _report_success(environment: str, target: str, progress: _Progress) -> None:
    noun = "migration" if progress.count == 1 else "migrations"
    if progress.direction == "upgrade" and progress.count == 0:
        ui.success(f"Nothing to apply; {environment} is up to date.")
    elif progress.direction == "upgrade":
        where = "at head" if target == "head" else f"at {target}"
        ui.success(f"Applied {progress.count} {noun}; {environment} is {where}.")
    elif progress.count == 0:
        ui.success("Nothing to revert.")
    else:
        ui.success(f"Reverted {progress.count} {noun}.")


def _report_failure(environment: str, progress: _Progress, verbose: bool) -> None:
    reason = _exception_message(progress.traceback) or progress.failure
    if progress.current:
        ui.error(f"{progress.current} failed")
    else:
        ui.error(f"{progress.direction.capitalize()} failed")
    if reason:
        ui.detail(reason, stderr=True)
    if progress.current:
        recorded = "applied" if progress.direction == "upgrade" else "reverted"
        ui.hint(
            f"It was not recorded as {recorded}. Anything it ran before the failure stays "
            f"in place, so make the SQL safe to re-run, then run `ch-migrate "
            f"{'up' if progress.direction == 'upgrade' else 'down'} {environment}` again.",
            stderr=True,
        )
    if verbose:
        for line in progress.traceback:
            click.echo(line, err=True)
    elif progress.traceback:
        ui.hint("Add `--verbose` to see the full traceback.", stderr=True)


def _exception_message(lines: list[str]) -> str:
    """The final exception of a Python traceback, without its module path."""
    last_frame = max((i for i, line in enumerate(lines) if line.startswith(" ")), default=-1)
    tail = "\n".join(line for line in lines[last_frame + 1 :] if line.strip()).strip()
    if not tail:
        return ""
    found = _EXCEPTION_LINE.match(tail)
    if found is None:
        return clean_database_error(tail)
    message = clean_database_error(found["message"])
    return message if found["kind"] in _SELF_DESCRIBING else f"{found['kind']}: {message}"
