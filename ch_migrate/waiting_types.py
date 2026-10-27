"""Waiting errors, invocation deadline, and bounded progress output."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ch_migrate.waiting_display import Progress, WaitDisplay


class WaitingError(RuntimeError):
    """Server work is unfinished; the revision must not be completed."""


class UnknownOutcome(WaitingError):
    """Available evidence cannot justify replay or completion."""


class WaitTimeout(WaitingError):
    """The invocation's waiting deadline expired without cancelling server work."""

    def __init__(self, message: str, pending: str, elapsed: float) -> None:
        super().__init__(message)
        self.pending, self.elapsed = pending, elapsed


class MutationFailed(WaitingError):
    """ClickHouse reports `latest_fail_reason` for a mutation this run owns or waits behind."""

    def __init__(self, message: str, failure: FailedMutation) -> None:
        super().__init__(message)
        self.failure = failure


@dataclass(frozen=True)
class FailedMutation:
    """What the operator needs: where it failed, why, and the scoped KILL statement."""

    table: str
    mutation: str
    host: str
    reason: str
    kill: str
    foreign: bool = False


@dataclass
class WaitBudget:
    timeout: float | None = None
    started: float = field(default_factory=time.monotonic)
    poll_interval: float = 0.2
    display: WaitDisplay = field(default_factory=WaitDisplay)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def check(self, pending: str) -> None:
        if self.timeout is not None and self.elapsed >= self.timeout:
            self.finish()
            raise WaitTimeout(
                f"Timed out after {self.elapsed:.1f}s; server work continues; "
                f"migration incomplete. Pending: {pending}",
                pending,
                self.elapsed,
            )

    def pause(self, pending: str, progress: Progress | None = None) -> None:
        """Show `progress` (or the pending text), then sleep one poll within the deadline."""
        self.check(pending)
        self.display.update(progress if progress is not None else pending)
        remaining = self.poll_interval
        if self.timeout is not None:
            remaining = min(remaining, max(0, self.timeout - self.elapsed))
        time.sleep(remaining)
        self.check(pending)

    def complete(self, message: str) -> None:
        self.display.complete(message)

    def finish(self) -> None:
        self.display.stop()
