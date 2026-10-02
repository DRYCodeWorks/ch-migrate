"""Spike env.py for the official clickhouse-connect dialect.

Same shape as clickhouse_alembic/env.py (NullPool, one connection for the
whole run, version_table_schema=DB) but WITHOUT bootstrap_version_table, so
the official ClickHouseImpl creates alembic_version itself.
"""
import os

from alembic import context
from logging.config import fileConfig
from sqlalchemy import create_engine, pool

import clickhouse_connect.cc_sqlalchemy.alembic  # noqa: F401  registers ClickHouseImpl

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

DB = os.environ["CH_DATABASE"]
URL = os.environ.get(
    "SPIKE_URL", f"clickhousedb://default:spike@127.0.0.1:18123/{DB}"
)


def run_migrations_online() -> None:
    connectable = create_engine(URL, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        client = connection.connection.dbapi_connection.client
        print(f"SPIKE client session_id = {client.get_client_setting('session_id')!r}")
        context.configure(
            connection=connection,
            target_metadata=None,
            version_table="alembic_version",
            version_table_schema=DB,
        )
        with context.begin_transaction():
            context.run_migrations()


def run_migrations_offline() -> None:
    context.configure(
        url=URL,
        target_metadata=None,
        literal_binds=True,
        version_table="alembic_version",
        version_table_schema=DB,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
