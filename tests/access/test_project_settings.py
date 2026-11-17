"""The config.yaml loader for `access`: the same keys as `get_env_config`, and no secrets."""

import pytest
import yaml

from ch_migrate.access.settings import ProjectSettings, SettingsError, load_settings


def write(tmp_path, config):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config))
    return path


def test_reads_one_environment_merged_over_defaults(tmp_path):
    path = write(
        tmp_path,
        {
            "project": {"name": "shop"},
            "defaults": {"migration_user": "shop_migrator", "admin_user": "shop_admin"},
            "environments": {
                "dev": {"database": "shop_dev", "mcp_user_name": "shop_mcp"},
                "production": {"database": "shop_prod", "admin_user": "prod_admin"},
            },
        },
    )
    assert load_settings("dev", path) == ProjectSettings(
        environment="dev",
        database="shop_dev",
        project="shop",
        migration_user="shop_migrator",
        admin_user="shop_admin",
        mcp_user_name="shop_mcp",
        environments=("dev", "production"),
    )
    assert load_settings("production", path).admin_user == "prod_admin"


def test_falls_back_as_0_8_does(tmp_path):
    path = write(
        tmp_path, {"environments": {"dev": {"database": "shop_dev", "user": "legacy_user"}}}
    )
    settings = load_settings("dev", path)
    assert (settings.project, settings.migration_user, settings.admin_user) == (
        "shop",
        "legacy_user",
        "default",
    )
    assert settings.migration_role == "shop_migration_role"
    assert settings.placeholders() == {
        "db": "shop_dev",
        "project": "shop",
        "env": "dev",
        "migration_role": "shop_migration_role",
    }


def test_migration_role_names_a_role_another_project_shares(tmp_path):
    """2.8: a second project directory names the role it shares, in defaults or per environment."""
    path = write(
        tmp_path,
        {
            "project": {"name": "reports"},
            "defaults": {
                "migration_user": "shop_migrator",
                "migration_role": "shop_migration_role",
            },
            "environments": {
                "dev": {"database": "reports_dev"},
                "production": {"database": "reports", "migration_role": "prod_role"},
            },
        },
    )
    dev = load_settings("dev", path)
    assert dev.migration_role == "shop_migration_role"
    assert dev.placeholders()["migration_role"] == "shop_migration_role"
    assert load_settings("production", path).migration_role == "prod_role"


def test_resolves_no_secret(tmp_path, monkeypatch):
    ssm = {"migration_password": "/shop/dev/migration", "etl_password": "/shop/dev/etl"}
    path = write(
        tmp_path,
        {"environments": {"dev": {"database": "shop_dev", "migration_user": "m", "ssm": ssm}}},
    )

    def no_secrets(*args, **kwargs):
        raise AssertionError("a secret was resolved")

    monkeypatch.setattr("ch_migrate.secrets.get_secret", no_secrets)
    monkeypatch.setattr("ch_migrate.config.get_secret", no_secrets)
    assert load_settings("dev", path).ssm == ssm  # paths as written, never read


@pytest.mark.parametrize(
    "config, error",
    [
        ({"environments": {"prod": {}}}, "Unknown environment: dev. Available: prod"),
        ({}, "Unknown environment: dev. Available: (none)"),
        (
            {"environments": {"dev": {"migration_user": "m"}}},
            "database is required in config.yaml for environment 'dev'",
        ),
        (
            {"environments": {"dev": {"database": "d"}}},
            "migration_user is required in config.yaml for environment 'dev'",
        ),
        (
            {"environments": {"dev": {"database": "shop-dev", "migration_user": "m"}}},
            "Invalid database name: 'shop-dev'. "
            "Must start with letter/underscore and contain only alphanumeric/underscore.",
        ),
        ([1, 2], "config.yaml must be a mapping"),
        ({"environments": ["dev"]}, "environments in config.yaml must be a mapping"),
        (
            {"environments": {"dev": "shop_dev"}},
            "environments.dev in config.yaml must be a mapping",
        ),
        (
            {"defaults": "x", "environments": {"dev": {}}},
            "defaults in config.yaml must be a mapping",
        ),
        (
            {"environments": {"dev": {"database": "d", "migration_user": "m", "ssm": "/a"}}},
            "environments.dev.ssm in config.yaml must be a mapping",
        ),
        (
            {
                "environments": {
                    "dev": {"database": "d", "migration_user": "m", "migration_role": "a-b"}
                }
            },
            "Invalid migration role: 'a-b'. "
            "Must start with letter/underscore and contain only alphanumeric/underscore.",
        ),
    ],
)
def test_errors(tmp_path, config, error):
    with pytest.raises(SettingsError) as raised:
        load_settings("dev", write(tmp_path, config))
    assert str(raised.value) == error


def test_a_missing_config_is_an_error(tmp_path):
    with pytest.raises(SettingsError) as raised:
        load_settings("dev", tmp_path / "config.yaml")
    assert str(raised.value) == f"Configuration file not found: {tmp_path / 'config.yaml'}"


def test_config_that_is_not_valid_yaml_is_an_error(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("environments:\n  dev: [\n")
    with pytest.raises(SettingsError) as raised:
        load_settings("dev", path)
    assert str(raised.value) == (
        "config.yaml: line 3, column 1: not valid YAML: expected the node content, but found "
        "'<stream end>'"
    )
