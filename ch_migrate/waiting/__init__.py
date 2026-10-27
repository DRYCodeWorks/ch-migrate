"""Waiting for the work migrations start: mutations and ON CLUSTER DDL.

`up` doesn't record a migration until the mutations and distributed DDL its
statements started have finished. `MigrationWaiter` (waiter.py) drives that from
Alembic's cursor events; mutations.py and ddl.py wait on the server, journal.py
records what each statement started, versions.py writes the version table, and
display.py shows progress. The design is docs/design/2026-10-02-waiting.md.
"""

from ch_migrate.waiting.waiter import MigrationWaiter

__all__ = ["MigrationWaiter"]
