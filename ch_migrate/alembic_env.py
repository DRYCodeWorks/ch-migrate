"""Session-safe Alembic runtime used by the versioned project environment shim."""

from __future__ import annotations

import logging
import os
from logging.config import fileConfig
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import clickhouse_connect.cc_sqlalchemy.alembic  # Registers the official implementation.
from alembic import context
from clickhouse_connect.cc_sqlalchemy.alembic.impl import ClickHouseImpl
from clickhouse_connect.driver.httputil import get_pool_manager
from dotenv import load_dotenv
from sqlalchemy import URL, Column, MetaData, String, Table, create_engine, pool, text
from sqlalchemy.sql.dml import Insert

from ch_migrate.config import get_env_config
from ch_migrate.hooks import HookRegistry, run_hooks
from ch_migrate.rebase import _literal_assignment
from ch_migrate.version_table import (
    VersionTableDDLCompiler,
    VersionTableState,
    assert_version_mutations_healthy,
    inspect_version_table,
    sync_version_replica,
)

ENV_VERSION = 2
DEFAULT_SESSION_TIMEOUT = 1800
logger = logging.getLogger(__name__)


class ChMigrateImpl(ClickHouseImpl):
    """Retain official version-table semantics while extending the runtime."""

    __dialect__ = "clickhousedb"

    def version_table_impl(self, *, version_table, version_table_schema, version_table_pk, **kw):
        state = self.context_opts.get("ch_migrate_version_state")
        if state is None:
            return super().version_table_impl(
                version_table=version_table,
                version_table_schema=version_table_schema,
                version_table_pk=version_table_pk,
                **kw,
            )
        return Table(
            version_table,
            MetaData(),
            Column("version_num", String(32), nullable=False),
            state.new_engine(),
            schema=version_table_schema,
            info={"ch_migrate_on_cluster": state.on_cluster},
        )

    def _exec(self, construct, execution_options=None, multiparams=None, params=None):
        if isinstance(construct, Insert) and self._is_version_table_construct(construct):
            self._insert_version_row(construct)
            return SimpleNamespace(rowcount=1)
        return super()._exec(
            construct, execution_options=execution_options, multiparams=multiparams, params=params
        )

    def _exec_version_update(self, construct, execution_options=None):
        # The official INSERT then waited DELETE, with the INSERT written by _insert_version_row.
        self._insert_version_row(construct)
        where = self._compile_version_where(construct)
        super()._exec(
            text(
                f"ALTER TABLE {self._version_table_name} DELETE WHERE {where} "
                "SETTINGS mutations_sync = 2"
            )
        )
        return SimpleNamespace(rowcount=1)

    def _insert_version_row(self, construct) -> None:
        # Replicated and Shared MergeTree silently drop a block identical to an earlier insert
        # (DRY-1447), so re-inserting a previous head after `down` left no version row at all.
        # A token unique to this write keeps every intentional version row.
        value = self._compile_clause(next(iter(construct._values.values())))
        token = f"'chm_version_{uuid4().hex}'"
        super()._exec(
            text(
                f"INSERT INTO {self._version_table_name} (version_num) "
                f"SETTINGS insert_deduplication_token = {token}, async_insert = 0 "
                f"VALUES ({value})"
            )
        )


def run() -> None:
    """Run the selected environment on one official-dialect session."""
    root = Path.cwd()
    load_dotenv(root / ".env.local")
    env_config = get_env_config(os.environ.get("CH_ENVIRONMENT", "dev"), root / "config.yaml")
    os.environ["CH_DATABASE"] = env_config["database"]
    if env_config.get("cluster"):
        os.environ["CH_CLUSTER"] = env_config["cluster"]
    else:
        os.environ.pop("CH_CLUSTER", None)
    if context.config.config_file_name is not None:
        fileConfig(context.config.config_file_name)
    if context.is_offline_mode():
        _run_offline(env_config)
    else:
        _run_online(env_config)


def has_current_env(path: Path) -> bool:
    """Read the session-safety marker without importing project code."""
    return (
        path.is_file()
        and _literal_assignment(path.read_text(), "CH_MIGRATE_ENV_VERSION") == ENV_VERSION
    )


def _run_offline(env_config: dict[str, Any]) -> None:
    context.configure(
        url=_url(env_config),
        target_metadata=None,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table="alembic_version",
        version_table_schema=env_config["database"],
        ch_migrate_version_state=VersionTableState(
            env_config["database"], "", env_config.get("cluster")
        ),
    )
    context.get_context().dialect.ddl_compiler = VersionTableDDLCompiler
    with context.begin_transaction():
        context.run_migrations()


def _run_online(env_config: dict[str, Any]) -> None:
    # The session lives on whichever replica its connection reached: ClickHouse Cloud routes
    # each TCP connection, not each session id. A pool of its own keeps the session on one
    # connection instead of sharing the driver's default pool with other in-process clients.
    # A dropped connection still fails closed through session_check.
    session_pool = get_pool_manager(maxsize=1)
    engine = create_engine(
        _url(env_config), poolclass=pool.NullPool, connect_args={"pool_mgr": session_pool}
    )
    try:
        with engine.connect() as connection:
            _run_connection(connection, env_config)
    finally:
        engine.dispose()
        session_pool.clear()


def _run_connection(connection, env_config: dict[str, Any]) -> None:
    hooks = HookRegistry.from_config(env_config.get("hooks"))
    database = env_config["database"]
    # Establish the session before requiring its existence on every later request.
    connection.exec_driver_sql("SELECT 1")
    client = connection.connection.dbapi_connection.client
    client.set_client_setting("session_check", 1)
    state = inspect_version_table(client, database, env_config.get("cluster"))
    assert_version_mutations_healthy(client, state)
    state = sync_version_replica(client, state)
    if warning := state.warning():
        logger.warning(warning)
    connection.dialect.ddl_compiler = VersionTableDDLCompiler
    context.configure(
        connection=connection,
        target_metadata=None,
        version_table="alembic_version",
        version_table_schema=database,
        on_version_apply=_post_migrate(hooks, database),
        ch_migrate_version_state=state,
    )
    with context.begin_transaction():
        run_hooks(connection, hooks.pre_migrate, db=database, phase="pre_migrate", revision="all")
        context.run_migrations()


def _url(env_config: dict[str, Any]) -> URL:
    secure = env_config.get("secure", True)
    return URL.create(
        "clickhousedb",
        username=env_config.get("migration_user") or env_config.get("user"),
        password=env_config["password"],
        host=env_config["host"],
        port=env_config.get("port", 8443 if secure else 8123),
        database=env_config["database"],
        query={
            "secure": "true" if secure else "false",
            "session_timeout": str(env_config.get("session_timeout", DEFAULT_SESSION_TIMEOUT)),
        },
    )


def _post_migrate(hooks: HookRegistry, database: str):
    def after_revision(ctx, step, heads, run_args):
        revision = (
            step.up_revision
            if step.is_upgrade
            else (step.down_revisions[0] if step.down_revisions else "unknown")
        )
        run_hooks(
            ctx.connection,
            hooks.post_migrate,
            db=database,
            phase="post_migrate",
            revision=revision,
        )

    return after_revision
