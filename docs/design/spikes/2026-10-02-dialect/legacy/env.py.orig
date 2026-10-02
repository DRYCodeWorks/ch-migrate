"""
ClickHouse Alembic Environment.

This module is copied to user projects and configures Alembic for ClickHouse.
It loads configuration from config.yaml and secrets from environment variables.

Why SQLAlchemy?
--------------
Alembic is built on SQLAlchemy as its core database abstraction layer. While we use
clickhouse-connect for direct ClickHouse operations (bootstrap, helpers), Alembic
requires SQLAlchemy for:
- Connection management and pooling
- Transaction handling (even though ClickHouse DDL isn't transactional)
- Database dialect registration (clickhouse-sqlalchemy provides the ClickHouse dialect)
- The `op.execute()` interface in migrations

The clickhouse-sqlalchemy package provides the `clickhouse+http://` URL dialect that
Alembic uses to connect to ClickHouse.
"""

import logging
import os
from logging.config import fileConfig
from pathlib import Path
from urllib.parse import quote_plus

from alembic import context
from alembic.ddl import impl
from dotenv import load_dotenv
from sqlalchemy import Connection, create_engine, pool, text

from clickhouse_alembic.config import get_env_config
from clickhouse_alembic.hooks import HookRegistry, run_hooks

logger = logging.getLogger(__name__)

# Alembic Config object
config = context.config

# Get environment name from CH_ENVIRONMENT (set by ch-migrate CLI)
env_name = os.environ.get("CH_ENVIRONMENT", "dev")

# Load .env.local for secrets (if it exists)
env_local = Path.cwd() / ".env.local"
if env_local.exists():
    load_dotenv(env_local)

# Load configuration
config_path = Path.cwd() / "config.yaml"
try:
    env_config = get_env_config(env_name, config_path)
except FileNotFoundError:
    raise FileNotFoundError(
        f"config.yaml not found in {Path.cwd()}\n"
        f"Run 'ch-migrate init' to create project structure."
    )

# Setup logging
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Load hook registry from config
hook_registry = HookRegistry.from_config(env_config.get("hooks"))


class ClickhouseImpl(impl.DefaultImpl):
    """Alembic implementation for ClickHouse dialect."""

    __dialect__ = "clickhouse"
    transactional_ddl = False


def get_sqlalchemy_url() -> str:
    """Build SQLAlchemy-compatible ClickHouse URL from config."""
    host = env_config["host"]
    # Support both new 'migration_user' and legacy 'user' field
    user = env_config.get("migration_user") or env_config.get("user")
    password = env_config["password"]
    database = env_config["database"]
    port = env_config.get("port", 8443)
    secure = env_config.get("secure", True)

    # URL-encode password to handle special characters
    encoded_password = quote_plus(password)

    # Build SQLAlchemy URL
    protocol_param = "?protocol=https" if secure else ""
    return f"clickhouse+http://{user}:{encoded_password}@{host}:{port}/{database}{protocol_param}"


# Export database name for use in migrations
DATABASE_NAME = env_config["database"]
os.environ["CH_DATABASE"] = DATABASE_NAME

# Export cluster name if configured (for ON CLUSTER support)
if env_config.get("cluster"):
    os.environ["CH_CLUSTER"] = env_config["cluster"]


def bootstrap_version_table(connection: Connection) -> None:
    """
    Create alembic_version table with ClickHouse-compatible engine.

    Must be called before Alembic tries to use its version table.
    The database must already exist (created by 'ch-migrate bootstrap').
    """
    db = DATABASE_NAME

    # Check if version table exists
    result = connection.execute(
        text(
            f"""
        SELECT count() FROM system.tables
        WHERE database = '{db}' AND name = 'alembic_version'
    """
        )
    )
    count = result.scalar()
    exists = count is not None and count > 0

    if not exists:
        # Create with ClickHouse Cloud compatible engine
        connection.execute(
            text(
                f"""
            CREATE TABLE {db}.alembic_version
            (
                `updated` DateTime DEFAULT now(),
                `version_num` String
            )
            ENGINE = ReplacingMergeTree()
            ORDER BY updated
            SETTINGS index_granularity = 8192
        """
            )
        )
        connection.commit()


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode - generates SQL without executing."""
    url = get_sqlalchemy_url()
    context.configure(
        url=url,
        target_metadata=None,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table="alembic_version",
        version_table_schema=DATABASE_NAME,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode - executes against the database."""
    connectable = create_engine(get_sqlalchemy_url(), poolclass=pool.NullPool)
    db = DATABASE_NAME

    def _on_version_apply(ctx, step, heads, run_args):
        """Fire post-migrate hooks after each migration step.

        on_version_apply fires AFTER each migration step completes,
        so we use it for post-migrate hooks only.
        """
        revision = step.up_revision if step.is_upgrade else (
            step.down_revisions[0] if step.down_revisions else "unknown"
        )
        if hook_registry.post_migrate:
            run_hooks(
                ctx.connection, hook_registry.post_migrate,
                db=db, phase="post_migrate", revision=revision,
            )

    with connectable.connect() as connection:
        bootstrap_version_table(connection)

        configure_kwargs = dict(
            connection=connection,
            target_metadata=None,
            version_table="alembic_version",
            version_table_schema=DATABASE_NAME,
        )

        if hook_registry.has_hooks:
            configure_kwargs["on_version_apply"] = _on_version_apply

        context.configure(**configure_kwargs)

        with context.begin_transaction():
            # Fire pre-migrate hooks before the migration run
            if hook_registry.pre_migrate:
                run_hooks(
                    connection, hook_registry.pre_migrate,
                    db=db, phase="pre_migrate", revision="all",
                )

            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
