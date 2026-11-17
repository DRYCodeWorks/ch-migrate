"""Shared fixtures for the offline access tests: a project's settings and its files."""

from __future__ import annotations

import textwrap

import pytest

from ch_migrate.access.loader import validate_access
from ch_migrate.access.settings import ProjectSettings

CONFIG_YAML = """\
project:
  name: shop
environments:
  dev:
    host: localhost
    database: shop_dev
    migration_user: shop_migrator
    admin_user: shop_admin
  production:
    host: localhost
    database: shop_prod
    migration_user: shop_migrator
    admin_user: shop_admin
"""


@pytest.fixture
def settings():
    return ProjectSettings(
        environment="dev",
        database="shop_dev",
        project="shop",
        migration_user="shop_migrator",
        admin_user="shop_admin",
        environments=("dev", "production"),
    )


@pytest.fixture
def validate(tmp_path, settings):
    """Write access.yaml (dedented) and validate it; returns the Validation."""

    def run(text, environment=None):
        (tmp_path / "access.yaml").write_text(textwrap.dedent(text))
        chosen = settings if environment is None else _for(settings, environment)
        return validate_access(chosen, tmp_path)

    return run


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project directory holding config.yaml, as the working directory."""
    (tmp_path / "config.yaml").write_text(CONFIG_YAML)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _for(settings, environment):
    database = {"dev": "shop_dev", "production": "shop_prod"}[environment]
    return ProjectSettings(
        environment=environment,
        database=database,
        project=settings.project,
        migration_user=settings.migration_user,
        admin_user=settings.admin_user,
        environments=settings.environments,
    )
