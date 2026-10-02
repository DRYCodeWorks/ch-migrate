"""
clickhouse-alembic: Alembic-based migrations for ClickHouse Cloud.

Usage:
    from clickhouse_alembic import read_sql, get_db, get_env_config, create_dictionary
"""

__version__ = "0.4.1"


from typing import Any


# Lazy imports to avoid import errors before dependencies are created
def __getattr__(name: str) -> Any:
    if name in ("read_sql", "get_db", "create_dictionary", "on_cluster", "get_cluster"):
        from clickhouse_alembic.helpers import (
            create_dictionary,
            get_cluster,
            get_db,
            on_cluster,
            read_sql,
        )

        return {
            "read_sql": read_sql,
            "get_db": get_db,
            "create_dictionary": create_dictionary,
            "on_cluster": on_cluster,
            "get_cluster": get_cluster,
        }[name]
    elif name == "run_sql":
        from clickhouse_alembic.sql import run_sql

        return run_sql
    elif name == "IrreversibleMigration":
        from clickhouse_alembic.downgrade import IrreversibleMigration

        return IrreversibleMigration
    elif name == "get_env_config":
        from clickhouse_alembic.config import get_env_config

        return get_env_config
    elif name in ("get_secret", "SSMSecretNotFoundError", "SSMJsonKeyError"):
        from clickhouse_alembic.secrets import SSMJsonKeyError, SSMSecretNotFoundError, get_secret

        return {
            "get_secret": get_secret,
            "SSMSecretNotFoundError": SSMSecretNotFoundError,
            "SSMJsonKeyError": SSMJsonKeyError,
        }[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "__version__",
    "read_sql",
    "run_sql",
    "IrreversibleMigration",
    "get_db",
    "get_env_config",
    "create_dictionary",
    "on_cluster",
    "get_cluster",
    "get_secret",
    "SSMSecretNotFoundError",
    "SSMJsonKeyError",
]
