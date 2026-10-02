"""Project Alembic entrypoint; implementation lives in clickhouse_alembic."""

from clickhouse_alembic.alembic_env import run

CH_MIGRATE_ENV_VERSION = 2

from ch_migrate.alembic_env import run

run()
