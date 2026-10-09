"""Shared ClickHouse connection helpers for CLI commands."""

from __future__ import annotations

import io
import sys
from contextlib import contextmanager
from typing import Any


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


def get_current_heads(env_config: dict[str, Any]) -> set[str]:
    """Query alembic_version table for current head revision(s).

    Alembic stores only the current head(s) in alembic_version, not
    every historically applied revision. Use resolve_applied_revisions()
    to expand these into the full set of applied revisions.

    Args:
        env_config: Environment config dict from get_env_config().

    Returns:
        Set of current head revision ID strings (usually just one).
    """
    from clickhouse_connect.driver.binding import quote_identifier

    with _suppress_stderr():
        client = get_client(env_config)
        try:
            db = env_config["database"]
            engines = client.query(
                "SELECT engine FROM system.tables "
                "WHERE database = {db:String} AND name = 'alembic_version'",
                parameters={"db": db},
            ).result_rows
            if not engines:
                return set()
            # Legacy tables require FINAL; the official plain MergeTree rejects it.
            final = " FINAL" if engines[0][0].endswith("ReplacingMergeTree") else ""
            result = client.query(
                f"SELECT version_num FROM {quote_identifier(db)}.alembic_version{final}"
            )
            return {row[0] for row in result.result_rows}
        finally:
            client.close()
