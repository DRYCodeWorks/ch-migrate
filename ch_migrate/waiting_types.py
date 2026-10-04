"""Waiting errors, invocation deadline, and bounded progress output."""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field


class WaitingError(RuntimeError):
    """Server work is unfinished; the revision must not be completed."""


class UnknownOutcome(WaitingError):
    """Available evidence cannot justify replay or completion."""


class WaitTimeout(WaitingError):
    """The invocation's waiting deadline expired without cancelling server work."""


@dataclass
class WaitBudget:
    timeout: float | None = None
    started: float = field(default_factory=time.monotonic)
    poll_interval: float = 0.2
    _last_line: str = ""
    _last_printed: float = 0
    _live: bool = False

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def check(self, pending: str) -> None:
        if self.timeout is not None and self.elapsed >= self.timeout:
            self.finish()
            raise WaitTimeout(
                f"Timed out after {self.elapsed:.1f}s; server work continues; "
                f"migration incomplete. Pending: {pending}"
            )

    def pause(self, pending: str) -> None:
        self.check(pending)
        self.progress(pending)
        remaining = self.poll_interval
        if self.timeout is not None:
            remaining = min(remaining, max(0, self.timeout - self.elapsed))
        time.sleep(remaining)
        self.check(pending)

    def progress(self, pending: str) -> None:
        now = time.monotonic()
        tty = sys.stderr.isatty()
        if pending != self._last_line or now - self._last_printed >= (0.5 if tty else 5):
            line = f"Waiting: {pending}; {self.elapsed:.1f}s elapsed"
            if tty:
                print("\r\033[K" + line, end="", file=sys.stderr, flush=True)
                self._live = True
            else:
                print(line, file=sys.stderr, flush=True)
            self._last_printed, self._last_line = now, pending

    def complete(self, message: str) -> None:
        self.finish()
        if self._last_line:
            print(f"Finished: {message}; {self.elapsed:.1f}s elapsed", file=sys.stderr, flush=True)
        self._last_line = ""

    def finish(self) -> None:
        if self._live:
            print(file=sys.stderr, flush=True)
            self._live = False
