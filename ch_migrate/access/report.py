"""How `access plan`, `apply`, `check` and `bootstrap` print what they do, in `ui`'s style.

A change made is its plan line on stdout. A failure is an error and a refused best-effort
grant a warning, both on stderr. On a cluster whose users and roles are on each node's own
disk, the node that was read is named on stdout, before any change (Decision 14). Where users
and roles are replicated but every replica can't be read at once, one line on stderr says
why, and another when a read is repeated for a replica that may be behind (2.5).
"""

from __future__ import annotations

from typing import Any

from rich.text import Text

from ch_migrate import ui
from ch_migrate.access.applier import Step
from ch_migrate.access.check import Checked
from ch_migrate.access.live import LOCAL_STORAGE, LiveState, Patience, Reader, read_node
from ch_migrate.access.planner import Change
from ch_migrate.access.server import address
from ch_migrate.access.settings import ProjectSettings

# How each marker of a plan line is coloured in a terminal.
MARKER_STYLES = {"+": "green", "-": "red", "~": "cyan", "!": "yellow"}


def report_step(step: Step) -> None:
    """One change as it is made: its line on stdout, a failure or a skip on stderr."""
    change = step.change
    if step.skipped:
        ui.warn(f"skipped: {change}, because {step.skipped}")
    elif step.failure is None:
        print_change(change)
    else:
        report = ui.warn if change.action == "best_effort" else ui.error
        report(f"{change}: {step.failure}")


def report_node(client: Any, settings: ProjectSettings, live: LiveState) -> None:
    """Name the node `live` was read from, where that is the only node read (Decision 14).

    With `cluster` set and users and roles on each node's own disk, `plan`, `apply` and
    `bootstrap` read only the node `client` is connected to. An ON CLUSTER change that
    failed on another node goes unseen there, so the line says to run `plan` against each
    node. Nothing prints on Keeper storage or on a single node, and legacy `bootstrap`
    never calls this.
    """
    if settings.server.cluster is None or live.storage != LOCAL_STORAGE:
        return
    node = read_node(client) or address(settings)
    ui.step(
        f"Read node {node} only: users and roles are on each node's own disk. After a failed "
        f"ON CLUSTER apply, run `ch-migrate access plan {settings.environment}` against each node."
    )


def report_check(checked: Checked, *, best_effort: bool) -> None:
    """Each required grant the migration user lacks, then the verdict.

    With `best_effort`, each best-effort grant it lacks is a warning too. `bootstrap` leaves
    them out, because it has already warned about them as the admin read them.
    """
    who = f"user {checked.user}"
    missing = checked.missing
    for verdict in missing:
        ui.error(f"{who} lacks {verdict}")
    for verdict in checked.shortfalls if best_effort else ():
        ui.warn(f"best-effort: {who} lacks {verdict}")
    count, role = len(missing), f"role {checked.role}"
    if count:
        ui.error(f"{who} lacks {count} of the {checked.required} required grants of {role}.")
    else:
        ui.success(f"{who} holds every required grant of {role} ({checked.required}).")


def reader(client: Any, settings: ProjectSettings) -> Reader:
    """How a command reads the server, as `client`'s login (2.5).

    Every replica at once where users and roles are replicated, through config.yaml's
    `cluster` or the one found. A replica that is behind is waited for up to
    `access_read_timeout`. When the replicas can't be read, one line on stderr says why
    only if a missing row makes the command wait. At the deadline, disagreements are named.
    """
    seconds = settings.server.read_timeout
    return Reader(
        client,
        Patience(seconds=seconds, on_wait=_say_waiting),
        cluster=settings.server.cluster,
        on_fallback=lambda reason: _say_fallback(reason, seconds),
        on_disagreement=lambda replicas: _say_disagreement(replicas, seconds),
    )


def print_change(change: Change) -> None:
    marker = (f"{change.marker} ", MARKER_STYLES[change.marker])
    ui.out.print(Text.assemble(marker, str(change)))


def _say_waiting(seconds: float) -> None:
    ui.step(
        f"Something isn't on the server yet. Reading again for up to {seconds:g} s, "
        "in case a replica is behind.",
        stderr=True,
    )


def _say_fallback(reason: str, seconds: float) -> None:
    ui.step(
        f"Can't read every replica at once, so a missing row is read again for up to "
        f"{seconds:g} s. {reason}",
        stderr=True,
    )


def _say_disagreement(replicas: tuple[str, ...], seconds: float) -> None:
    names = ", ".join(replicas[:-1]) + f" and {replicas[-1]}" if len(replicas) > 1 else replicas[0]
    ui.step(
        f"Replicas {names} still disagree after {seconds:g} s, so what the node connected to "
        "holds counts.",
        stderr=True,
    )
