"""One waiting deadline per invocation, and the poll between checks."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ch_migrate.waiting.display import Progress, WaitDisplay
from ch_migrate.waiting.errors import WaitTimeout


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
