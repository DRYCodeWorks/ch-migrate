"""Hold each migration until ClickHouse has finished the work its statements started."""

from __future__ import annotations

from sqlalchemy import event

from ch_migrate.waiting.budget import WaitBudget
from ch_migrate.waiting.mutations import MutationWaiter
from ch_migrate.waiting.sql import is_write

# ON CLUSTER statements wait inside ClickHouse. The limit stays below clickhouse-connect's
# default 300-second read timeout, so ClickHouse answers before the client gives up.
DDL_WAIT_LIMIT = 270


class MigrationWaiter:
    """Waits after every write on the migration connection; reads and `SET`s pass through.

    ON CLUSTER statements return only once every host has run them; a host that fails or
    runs out of time is an error. After each write it waits for the database's unfinished
    mutations.
    """

    def __init__(self, connection, state, timeout: float | None = None):
        self.connection = connection
        self.client = connection.connection.dbapi_connection.client
        self.budget = WaitBudget(timeout)
        self.mutations = MutationWaiter(self.client, state, self.budget)
        self._installed = False

    def install(self, config) -> None:
        # `ch-migrate up` passes the display it labels with each migration's name.
        self.budget.display = config.attributes.get("ch_migrate_display", self.budget.display)
        # Request settings, not a session SET, so a migration's own SETs can't undo them.
        # wait_end_of_query makes a DDL timeout arrive as an error, not a truncated response.
        self.client.set_client_setting("distributed_ddl_output_mode", "throw")
        self.client.set_client_setting("distributed_ddl_task_timeout", DDL_WAIT_LIMIT)
        self.client.set_client_setting("wait_end_of_query", 1)
        event.listen(self.connection, "before_cursor_execute", self._before)
        event.listen(self.connection, "after_cursor_execute", self._after)
        self._installed = True

    def wait(self, before_run: bool = False) -> None:
        """Wait for the database's unfinished mutations, including another writer's."""
        self.mutations.wait(before_run)

    def close(self) -> None:
        self.budget.finish()
        if self._installed:
            event.remove(self.connection, "before_cursor_execute", self._before)
            event.remove(self.connection, "after_cursor_execute", self._after)
            self._installed = False

    def _before(self, conn, cursor, statement, parameters, context, executemany) -> None:
        if is_write(statement) and self.budget.timeout is not None:
            self._limit_distributed_ddl()

    def _after(self, conn, cursor, statement, parameters, context, executemany) -> None:
        if is_write(statement):
            self.wait()

    def _limit_distributed_ddl(self) -> None:
        # `--timeout` covers the whole run, so ClickHouse waits only for what's left of it.
        self.budget.check("the next statement")
        left = int(self.budget.timeout - self.budget.elapsed)
        self.client.set_client_setting(
            "distributed_ddl_task_timeout", max(1, min(DDL_WAIT_LIMIT, left))
        )
