"""What `up` shows while it waits for mutations.

Waiters hand over what the server reports; this module only draws it, and nothing
here decides whether work is done. On a terminal each wait is a live bar on
stderr, weighted by bytes when part sizes are known and labelled with parts. Piped
output, for CI logs and agents, gets plain lines instead: one when a wait starts,
one at each 10% step or every 30 seconds, and one when it finishes.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Union

from rich.console import Console, Group
from rich.live import Live
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

from ch_migrate import ui

PIPED_INTERVAL = 30.0
PIPED_STEPS = 10
BAR_WIDTH = 38

# What a waiter reports: a mutation, or a plain status line.
Progress = Union["MutationProgress", str]


class WaitDisplay:
    """Draws one wait at a time; `complete` prints its result line, `stop` clears it."""

    def __init__(self, console: Console | None = None) -> None:
        self.console = console or ui.err
        self.migration = ""
        self._wait: _Wait | None = None
        self._live: Live | None = None
        self._parts_only = False

    def start_migration(self, label: str) -> None:
        self.migration = label

    @property
    def wants_bytes(self) -> bool:
        """Only the terminal bar shows bytes, so piped runs skip the size lookup."""
        return self._terminal and not self._parts_only

    def update(self, progress: Progress) -> None:
        wait = self._wait_for(progress)
        wait.observe(progress)
        if self._terminal:
            self._draw_live(wait)
        else:
            self._print_piped(wait)

    def parts_only(self, reason: str) -> None:
        """Part sizes can't be read; say so once, then count parts."""
        if self._parts_only:
            return
        self._parts_only = True
        self.console.print(
            Text.assemble(("! ", "yellow"), (f"Progress counts parts only: {reason}", "yellow"))
        )

    def complete(self, fallback: str) -> None:
        wait = self._wait
        self.stop()
        if wait is not None:
            self.console.print(
                Text.assemble(("✓ ", "green"), wait.summary(self.migration, fallback))
            )

    def stop(self) -> None:
        if self._live is not None:
            self._live.stop()
            self._live = None
        self._wait = None

    def _wait_for(self, progress: Progress) -> _Wait:
        key = _key(progress)
        if self._wait is None or self._wait.key != key:
            self.stop()
            self._wait = _Wait(key, started=time.monotonic())
        return self._wait

    @property
    def _terminal(self) -> bool:
        # A dumb terminal can't redraw in place, so it gets the piped lines.
        return self.console.is_terminal and not self.console.is_dumb_terminal

    def _draw_live(self, wait: _Wait) -> None:
        if self._live is None:
            self._live = Live(console=self.console, auto_refresh=False, transient=True)
            self._live.start()
        self._live.update(wait.renderable(), refresh=True)

    def _print_piped(self, wait: _Wait) -> None:
        line = wait.piped_line(time.monotonic())
        if line is not None:
            self.console.print(Text("  " + line))


@dataclass(frozen=True)
class MutationProgress:
    """The mutation a statement started, on every host that hasn't finished it."""

    table: str
    mutation: str
    hosts: tuple[HostParts, ...]


@dataclass(frozen=True)
class HostParts:
    """One host's remaining parts. `sizes` maps each remaining part to its bytes, if known."""

    host: str
    names: frozenset[str]
    count: int
    sizes: dict[str, int] | None = None

    @property
    def bytes_left(self) -> int | None:
        if self.sizes is None or not self.names.issubset(self.sizes):
            return None
        return sum(self.sizes[name] for name in self.names)


@dataclass
class _Wait:
    key: str
    started: float
    latest: Progress = ""
    parts_total: dict[str, int] = field(default_factory=dict)
    bytes_total: dict[str, int] = field(default_factory=dict)
    printed_at: float | None = None
    printed_step: int = -1

    def observe(self, progress: Progress) -> None:
        self.latest = progress
        if isinstance(progress, MutationProgress):
            for host in progress.hosts:
                self.parts_total[host.host] = max(self.parts_total.get(host.host, 0), host.count)
                if host.host not in self.bytes_total and host.bytes_left is not None:
                    self.bytes_total[host.host] = host.bytes_left

    def renderable(self):
        elapsed = _duration(time.monotonic() - self.started)
        progress = self.latest
        if isinstance(progress, str):
            return Text(f"  waiting: {progress} · {elapsed}", style="dim")
        rows = [self._host_row(host, len(progress.hosts) > 1) for host in progress.hosts]
        return Group(*rows)

    def piped_line(self, now: float) -> str | None:
        step = int(self._fraction() * PIPED_STEPS)
        due = self.printed_at is None or now - self.printed_at >= PIPED_INTERVAL
        if not due and step <= self.printed_step:
            return None
        first = self.printed_at is None
        self.printed_at, self.printed_step = now, step
        elapsed = "" if first else f" · {_duration(now - self.started)}"
        return self._piped_text() + elapsed

    def summary(self, migration: str, fallback: str) -> str:
        elapsed = _duration(time.monotonic() - self.started)
        progress = self.latest
        if isinstance(progress, str):
            return f"{migration or fallback}  waited for {progress} · {elapsed}"
        return f"{migration or progress.table}  {self._mutation_totals(progress)} · {elapsed}"

    def _host_row(self, host: HostParts, named: bool):
        total = self.parts_total.get(host.host, host.count)
        parts = f"{total - host.count}/{total} parts"
        left, size_total = host.bytes_left, self.bytes_total.get(host.host)
        if left is None or not size_total:
            return _bar_row(host.host if named else "", _bar(total - host.count, total), parts)
        done = size_total - left
        stats = (
            f"{_sizes(done, size_total)} · {parts} · {_duration(time.monotonic() - self.started)}"
        )
        if eta := self._eta(done, left):
            stats += f" · ~{eta} left"
        return _bar_row(host.host if named else "", _bar(done, size_total), stats)

    def _eta(self, done: int, left: int) -> str:
        elapsed = time.monotonic() - self.started
        if done <= 0 or elapsed < 2:
            return ""
        return _duration(left / (done / elapsed))

    def _fraction(self) -> float:
        progress = self.latest
        if isinstance(progress, str):
            return 0.0
        total = sum(self.parts_total.values())
        left = sum(host.count for host in progress.hosts)
        return (total - left) / total if total else 0.0

    def _piped_text(self) -> str:
        progress = self.latest
        if isinstance(progress, str):
            return f"waiting: {progress}"
        total = sum(self.parts_total.values())
        done = total - sum(host.count for host in progress.hosts)
        replicas = f" on {len(progress.hosts)} replicas" if len(progress.hosts) > 1 else ""
        return f"waiting on {_named(progress.mutation)} ({progress.table}): {done}/{total} parts{replicas}"

    def _mutation_totals(self, progress: MutationProgress) -> str:
        parts = sum(self.parts_total.values())
        text = f"{_named(progress.mutation)} · {parts} {'part' if parts == 1 else 'parts'}"
        if self.bytes_total and len(self.bytes_total) == len(self.parts_total):
            text += f" · {_size(sum(self.bytes_total.values()))}"
        if len(self.parts_total) > 1:
            text += f" · {len(self.parts_total)} replicas"
        return text


def _key(progress: Progress) -> str:
    if isinstance(progress, MutationProgress):
        return f"mutation {progress.table} {progress.mutation}"
    return "status"


def _bar_row(label: str, bar: ProgressBar, stats: str) -> Table:
    row = Table.grid(padding=(0, 1))
    row.add_column(no_wrap=True)
    row.add_column(width=BAR_WIDTH)
    row.add_column(no_wrap=True)
    row.add_row(Text(f"  {label}".rstrip() if label else " "), bar, Text(stats))
    return row


def _bar(completed: float, total: float) -> ProgressBar:
    return ProgressBar(total=max(total, 1), completed=min(completed, total), width=BAR_WIDTH)


def _named(mutation: str) -> str:
    # MergeTree ids already read "mutation_7.txt"; replicated ones are bare numbers.
    return mutation if mutation.startswith("mutation") else f"mutation {mutation}"


def _duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _sizes(done: int, total: int) -> str:
    unit, scale = _unit(total)
    return f"{done / scale:.1f} / {total / scale:.1f} {unit}"


def _size(total: int) -> str:
    unit, scale = _unit(total)
    return f"{total / scale:.1f} {unit}"


def _unit(total: int) -> tuple[str, int]:
    for unit, scale in (("GiB", 1 << 30), ("MiB", 1 << 20), ("KiB", 1 << 10)):
        if total >= scale:
            return unit, scale
    return "B", 1
