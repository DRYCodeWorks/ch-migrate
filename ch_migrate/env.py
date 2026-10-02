"""Project Alembic entrypoint; implementation lives in ch_migrate."""

from ch_migrate.alembic_env import run

CH_MIGRATE_ENV_VERSION = 2


run()
