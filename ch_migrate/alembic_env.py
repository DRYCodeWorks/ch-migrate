"""Session-safe Alembic runtime used by the versioned project environment shim."""

from __future__ import annotations

import logging
import os
from logging.config import fileConfig
from pathlib import Path
from typing import Any

import clickhouse_connect.cc_sqlalchemy.alembic  # Registers the official implementation.
from alembic import context
from clickhouse_connect.cc_sqlalchemy.alembic.impl import ClickHouseImpl
from dotenv import load_dotenv
from sqlalchemy import URL, Column, MetaData, String, Table, create_engine, pool
from sqlalchemy.sql.dml import Delete, Insert, Update

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
from ch_migrate.waiting import MigrationWaiter

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
        # The official implementation owns these SQLAlchemy version constructs;
        # all user SQL, including raw bind execution, is covered by cursor events.
        waiter = self.context_opts.get("ch_migrate_waiter")
        if (
            waiter is not None
            and waiter.revision is not None
            and isinstance(construct, (Insert, Update, Delete))
            and self._is_version_table_construct(construct)
        ):
            return waiter.versions.execute(construct, self)
        return super()._exec(
            construct, execution_options=execution_options, multiparams=multiparams, params=params
        )


def run() -> None:
    """Run the selected environment on one official-dialect session."""
    root = Path.cwd()
    load_dotenv(root / ".env.local")
    environment = context.config.attributes.get(
        "ch_migrate_environment", os.environ.get("CH_ENVIRONMENT", "dev")
    )
    env_config = get_env_config(environment, root / "config.yaml")
    os.environ["CH_DATABASE"] = env_config["database"]
    if env_config.get("cluster"):
        os.environ["CH_CLUSTER"] = env_config["cluster"]
    else:
        os.environ.pop("CH_CLUSTER", None)
    if context.config.config_file_name is not None:
        fileConfig(context.config.config_file_name, disable_existing_loggers=False)
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
    engine = create_engine(_url(env_config), poolclass=pool.NullPool)
    try:
        with engine.connect() as connection:
            _run_connection(connection, env_config)
    finally:
        engine.dispose()


def _run_connection(connection, env_config: dict[str, Any]) -> None:
    # The checked session and listener registration share this failure boundary.
    connection.exec_driver_sql("SELECT 1")
    client = connection.connection.dbapi_connection.client
    client.set_client_setting("session_check", 1)
    state = inspect_version_table(client, env_config["database"], env_config.get("cluster"))
    assert_version_mutations_healthy(client, state)
    state = sync_version_replica(client, state)
    if warning := state.warning():
        logger.warning(warning)
    connection.dialect.ddl_compiler = VersionTableDDLCompiler
    waiter = MigrationWaiter(connection, state, context.config.attributes.get("ch_migrate_timeout"))
    try:
        waiter.install(context.config)
        context.configure(
            connection=connection,
            target_metadata=None,
            version_table="alembic_version",
            version_table_schema=state.database,
            ch_migrate_waiter=waiter,
            ch_migrate_version_state=state,
        )
        _run_context(waiter, HookRegistry.from_config(env_config.get("hooks")))
    finally:
        waiter.close()


def _run_context(waiter: MigrationWaiter, hooks: HookRegistry) -> None:
    runtime = context.get_context()
    runtime._migrations_fn = waiter.steps(runtime._migrations_fn, hooks)
    with context.begin_transaction():
        if _is_upgrade():
            waiter.versions.resume()
            waiter.run_pre_hooks(hooks)
        else:
            run_hooks(
                waiter.connection,
                hooks.pre_migrate,
                db=waiter.state.database,
                phase="pre_migrate",
                revision="all",
            )
        context.run_migrations()
        waiter.finish_run()


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


def _is_upgrade() -> bool:
    explicit = context.config.attributes.get("ch_migrate_command")
    if explicit is not None:
        return explicit == "upgrade"
    command = getattr(context.config.cmd_opts, "cmd", None)
    return bool(command and command[0].__name__ == "upgrade")
