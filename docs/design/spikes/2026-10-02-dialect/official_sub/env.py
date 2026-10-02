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
from clickhouse_connect.cc_sqlalchemy.alembic import ClickHouseImpl
from clickhouse_connect.cc_sqlalchemy.ddl.tableengine import ReplicatedMergeTree
from sqlalchemy import Column, MetaData, String, Table


class ChMigrateImpl(ClickHouseImpl):
    """Spike: subclass the official impl to own version-table DDL and see every statement."""

    __dialect__ = "clickhousedb"

    def version_table_impl(self, *, version_table, version_table_schema, version_table_pk, **kw):
        return Table(version_table, MetaData(), Column("version_num", String(32), nullable=False),
                     ReplicatedMergeTree(order_by="version_num"), schema=version_table_schema)

    def _exec(self, construct, execution_options=None, multiparams=None, params=None):
        print(f"HOOK _exec <- {type(construct).__name__}: {str(construct).strip()[:90]!r}")
        return super()._exec(construct, execution_options=execution_options,
                             multiparams=multiparams, params=params)

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
