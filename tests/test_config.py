"""Tests for configuration loading."""

from pathlib import Path

import pytest

from ch_migrate.config import get_env_config, load_config


class TestLoadConfig:
    def test_loads_yaml_config(self, tmp_path: Path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            """
project:
  name: test-project

defaults:
  port: 8443
  secure: true

environments:
  dev:
    host: dev.clickhouse.cloud
    database: testdb
    user: service_dev
"""
        )
        config = load_config(config_file)

        assert config["project"]["name"] == "test-project"
        assert config["defaults"]["port"] == 8443
        assert config["environments"]["dev"]["host"] == "dev.clickhouse.cloud"

    def test_raises_on_missing_file(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            load_config(tmp_path / "nonexistent.yaml")


class TestGetEnvConfig:
    def test_merges_defaults_with_environment(self, tmp_path: Path, monkeypatch):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            """
defaults:
  port: 8443
  secure: true
  admin_user: default

environments:
  dev:
    host: dev.clickhouse.cloud
    database: testdb
    user: service_dev
  prod:
    host: prod.clickhouse.cloud
    database: proddb
    user: service_prod
    port: 9443
"""
        )
        monkeypatch.setenv("CH_DEV_PASSWORD", "dev-pass")
        monkeypatch.setenv("CH_DEV_ADMIN_PASSWORD", "admin-pass")

        env_config = get_env_config("dev", config_file)

        assert env_config["host"] == "dev.clickhouse.cloud"
        assert env_config["port"] == 8443  # from defaults
        assert env_config["password"] == "dev-pass"
        assert env_config["admin_password"] == "admin-pass"

    def test_environment_overrides_defaults(self, tmp_path: Path, monkeypatch):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            """
defaults:
  port: 8443

environments:
  prod:
    host: prod.clickhouse.cloud
    database: proddb
    user: service_prod
    port: 9443
"""
        )
        monkeypatch.setenv("CH_PROD_PASSWORD", "prod-pass")

        env_config = get_env_config("prod", config_file)

        assert env_config["port"] == 9443  # overridden

    def test_raises_on_missing_password(self, tmp_path: Path, monkeypatch):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            """
environments:
  dev:
    host: dev.clickhouse.cloud
    database: testdb
    user: service_dev
"""
        )
        # Don't set CH_DEV_MIGRATION_PASSWORD or CH_DEV_PASSWORD

        with pytest.raises(ValueError, match="CH_DEV_MIGRATION_PASSWORD"):
            get_env_config("dev", config_file)

    def test_raises_on_unknown_environment(self, tmp_path: Path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            """
environments:
  dev:
    host: dev.clickhouse.cloud
"""
        )

        with pytest.raises(ValueError, match="Unknown environment"):
            get_env_config("staging", config_file)
