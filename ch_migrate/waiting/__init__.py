"""Waiting for the work migrations start: mutations and ON CLUSTER DDL.

`up` doesn't record a migration until the mutations and distributed DDL its statements
started have finished. `MigrationWaiter` (waiter.py) hooks the migration connection's
cursor events: before each write it bounds ClickHouse's own distributed DDL wait, and
after it, mutations.py waits until the database has no unfinished mutations on any
replica that must apply them. display.py shows the progress; budget.py holds the
invocation's `--timeout`.
"""

from ch_migrate.waiting.waiter import MigrationWaiter

__all__ = ["MigrationWaiter"]
