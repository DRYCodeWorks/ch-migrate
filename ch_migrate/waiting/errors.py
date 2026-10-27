"""Why waiting stopped short: a deadline, a failed mutation, or a deployment it can't check."""

from __future__ import annotations

from dataclasses import dataclass


class WaitingError(RuntimeError):
    """Server work is unfinished; the revision must not be completed."""


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
