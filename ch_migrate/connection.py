"""Shared ClickHouse connection helpers for CLI commands."""

from __future__ import annotations

import io
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from ch_migrate.version_table import (
    VersionTableState,
    assert_version_mutations_healthy,
    inspect_version_table,
    sync_version_replica,
)


@dataclass(frozen=True)
class MigrationState:
    heads: set[str]
    version_table: VersionTableState


@contextmanager
def _suppress_stderr():
    """Suppress stderr during clickhouse_connect operations.

    clickhouse_connect prints "Unexpected Http Driver Exception" directly
    to stderr on connection failures, bypassing the logging framework.
    """
    old_stderr = sys.stderr
    sys.stderr = io.StringIO()
    try:
        yield
    finally:
        sys.stderr = old_stderr


def get_client(env_config: dict[str, Any]) -> Any:
    """Create a clickhouse_connect client using migration user credentials.

    Args:
        env_config: Environment config dict from get_env_config().

    Returns:
        A clickhouse_connect Client instance.
    """
    import clickhouse_connect

    secure = env_config.get("secure", True)
    return clickhouse_connect.get_client(
        host=env_config["host"],
        port=env_config.get("port", 8443 if secure else 8123),
        username=env_config.get("migration_user") or env_config.get("user", ""),
        password=env_config.get("password", ""),
        secure=secure,
        interface="https" if secure else "http",
        connect_timeout=10,
        send_receive_timeout=15,
    )


def get_migration_state(env_config: dict[str, Any]) -> MigrationState:
    """Read heads and deployment state after the selected replica has caught up."""
    from clickhouse_connect.driver.binding import quote_identifier

    with _suppress_stderr():
        client = get_client(env_config)
        try:
            db = env_config["database"]
            state = inspect_version_table(client, db, env_config.get("cluster"))
            assert_version_mutations_healthy(client, state)
            state = sync_version_replica(client, state)
            if not state.table_engine:
                return MigrationState(set(), state)
            # Preserve legacy ReplacingMergeTree reads without applying FINAL to MergeTree.
            final = " FINAL" if state.table_engine.endswith("ReplacingMergeTree") else ""
            result = client.query(
                f"SELECT version_num FROM {quote_identifier(db)}.alembic_version{final}"
            )
            return MigrationState({row[0] for row in result.result_rows}, state)
        finally:
            client.close()
