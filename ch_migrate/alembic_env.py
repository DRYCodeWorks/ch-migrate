"""Session-safe Alembic runtime used by the versioned project environment shim."""

from __future__ import annotations

import os
from logging.config import fileConfig
from pathlib import Path
from typing import Any

import clickhouse_connect.cc_sqlalchemy.alembic  # Registers the official implementation.
from alembic import context
from clickhouse_connect.cc_sqlalchemy.alembic.impl import ClickHouseImpl
from dotenv import load_dotenv
from sqlalchemy import URL, create_engine, pool

from ch_migrate.config import get_env_config
from ch_migrate.hooks import HookRegistry, run_hooks
from ch_migrate.rebase import _literal_assignment

ENV_VERSION = 2
DEFAULT_SESSION_TIMEOUT = 1800


class ChMigrateImpl(ClickHouseImpl):
    """Retain official version-table semantics while extending the runtime."""

    __dialect__ = "clickhousedb"


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
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_online(env_config: dict[str, Any]) -> None:
    engine = create_engine(_url(env_config), poolclass=pool.NullPool)
    hooks = HookRegistry.from_config(env_config.get("hooks"))
    database = env_config["database"]
    try:
        with engine.connect() as connection:
            # Establish the session before requiring its existence on every later request.
            connection.exec_driver_sql("SELECT 1")
            client = connection.connection.dbapi_connection.client
            client.set_client_setting("session_check", 1)
            context.configure(
                connection=connection,
                target_metadata=None,
                version_table="alembic_version",
                version_table_schema=database,
                on_version_apply=_post_migrate(hooks, database),
            )
            with context.begin_transaction():
                run_hooks(
                    connection, hooks.pre_migrate, db=database, phase="pre_migrate", revision="all"
                )
                context.run_migrations()
    finally:
        engine.dispose()


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
