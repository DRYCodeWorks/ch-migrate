"""Tests for bootstrap module."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from clickhouse_alembic.bootstrap import (
    build_bootstrap_sql,
    escape_sql_string,
    run_bootstrap,
    validate_identifier,
)


class TestEscapeSqlString:
    def test_escapes_single_quotes(self):
        assert escape_sql_string("pass'word") == "pass''word"

    def test_escapes_backslashes(self):
        assert escape_sql_string("pass\\word") == "pass\\\\word"

    def test_handles_both(self):
        assert escape_sql_string("it's a\\test") == "it''s a\\\\test"


class TestValidateIdentifier:
    def test_accepts_valid_identifier(self):
        assert validate_identifier("myproject", "test") == "myproject"

    def test_accepts_identifier_with_underscore(self):
        assert validate_identifier("my_project_dev", "test") == "my_project_dev"

    def test_accepts_identifier_starting_with_underscore(self):
        assert validate_identifier("_private", "test") == "_private"

    def test_accepts_identifier_with_numbers(self):
        assert validate_identifier("project123", "test") == "project123"

    def test_rejects_identifier_starting_with_number(self):
        with pytest.raises(ValueError, match="Invalid test"):
            validate_identifier("123project", "test")

    def test_rejects_identifier_with_spaces(self):
        with pytest.raises(ValueError, match="Invalid database name"):
            validate_identifier("my project", "database name")

    def test_rejects_identifier_with_semicolon(self):
        with pytest.raises(ValueError, match="Invalid database name"):
            validate_identifier("mydb; DROP DATABASE prod; --", "database name")

    def test_rejects_identifier_with_dash(self):
        with pytest.raises(ValueError, match="Invalid project name"):
            validate_identifier("my-project", "project name")

    def test_rejects_empty_identifier(self):
        with pytest.raises(ValueError, match="Invalid user"):
            validate_identifier("", "user")


class TestBuildBootstrapSql:
    def test_includes_database_creation(self):
        sql = build_bootstrap_sql(
            project="myproject",
            db="myproject_dev",
            migration_user="migration_dev",
            migration_password="secret123",
        )
        assert "CREATE DATABASE IF NOT EXISTS myproject_dev" in sql

    def test_includes_migration_role_and_user(self):
        sql = build_bootstrap_sql(
            project="myproject",
            db="myproject_dev",
            migration_user="migration_dev",
            migration_password="secret123",
        )
        assert "CREATE ROLE IF NOT EXISTS myproject_migration_role" in sql
        assert "CREATE USER IF NOT EXISTS migration_dev" in sql
        assert "GRANT myproject_migration_role TO migration_dev" in sql

    def test_includes_full_user_role_management_grants(self):
        """Test that migration role has full user/role management privileges."""
        sql = build_bootstrap_sql(
            project="myproject",
            db="myproject_dev",
            migration_user="migration_dev",
            migration_password="secret123",
        )
        # Role management
        assert "GRANT CREATE ROLE, ALTER ROLE, DROP ROLE ON *.* TO myproject_migration_role" in sql
        # ROLE ADMIN for granting roles to users
        assert "GRANT ROLE ADMIN ON *.* TO myproject_migration_role" in sql

    def test_includes_system_table_grants(self):
        """Test system introspection grants target the shared migration role."""
        sql = build_bootstrap_sql(
            project="myproject",
            db="myproject_dev",
            migration_user="migration_dev",
            migration_password="secret123",
        )
        assert (
            "GRANT CURRENT GRANTS(SELECT ON system.*) TO myproject_migration_role WITH GRANT OPTION"
            in sql
        )
        assert "GRANT SELECT ON system.grants TO myproject_migration_role;" in sql
        assert "GRANT SELECT ON system.grants TO migration_dev" not in sql

    def test_excludes_dict_reader_when_not_configured(self):
        sql = build_bootstrap_sql(
            project="myproject",
            db="myproject_dev",
            migration_user="migration_dev",
            migration_password="secret123",
        )
        assert "dict_reader" not in sql.lower()

    def test_includes_dict_reader_when_configured(self):
        sql = build_bootstrap_sql(
            project="myproject",
            db="myproject_dev",
            migration_user="migration_dev",
            migration_password="secret123",
            dict_reader_name="dict_reader",
            dict_reader_password="dictpass",
        )
        assert "CREATE USER IF NOT EXISTS dict_reader" in sql
        assert "myproject_dict_role" in sql

    def test_excludes_mcp_user_when_not_configured(self):
        sql = build_bootstrap_sql(
            project="myproject",
            db="myproject_dev",
            migration_user="migration_dev",
            migration_password="secret123",
        )
        assert "mcp" not in sql.lower()

    def test_includes_mcp_user_when_configured(self):
        sql = build_bootstrap_sql(
            project="myproject",
            db="myproject_dev",
            migration_user="migration_dev",
            migration_password="secret123",
            mcp_user_name="mcp_reader",
            mcp_password="mcppass",
        )
        assert "CREATE USER IF NOT EXISTS mcp_reader" in sql
        assert "myproject_readonly_role" in sql
        assert "GRANT SELECT ON myproject_dev.*" in sql

    def test_escapes_passwords_in_sql(self):
        sql = build_bootstrap_sql(
            project="myproject",
            db="myproject_dev",
            migration_user="migration_dev",
            migration_password="pass'word",
        )
        assert "pass''word" in sql
        assert "pass'word" not in sql

    def test_rejects_invalid_database_name(self):
        with pytest.raises(ValueError, match="Invalid database name"):
            build_bootstrap_sql(
                project="myproject",
                db="mydb; DROP DATABASE prod; --",
                migration_user="migration_dev",
                migration_password="secret",
            )

    def test_rejects_invalid_project_name(self):
        with pytest.raises(ValueError, match="Invalid project name"):
            build_bootstrap_sql(
                project="my-project",
                db="myproject_dev",
                migration_user="migration_dev",
                migration_password="secret",
            )

    def test_rejects_invalid_migration_user(self):
        with pytest.raises(ValueError, match="Invalid migration user"):
            build_bootstrap_sql(
                project="myproject",
                db="myproject_dev",
                migration_user="user with spaces",
                migration_password="secret",
            )


class TestRunBootstrap:
    """Integration tests for run_bootstrap() with mocked connections."""

    @pytest.fixture
    def mock_env_config(self):
        """Return a minimal environment config."""
        return {
            "host": "localhost",
            "database": "myproject_dev",
            "migration_user": "migration_dev",
            "project": "myproject",
            "secure": False,
            "port": 8123,
        }

    @pytest.fixture
    def mock_clickhouse_connect(self):
        """Mock clickhouse_connect module for lazy import."""
        mock_module = MagicMock()
        mock_client = MagicMock()
        mock_module.get_client.return_value = mock_client
        return mock_module, mock_client

    @patch("clickhouse_alembic.bootstrap.get_secret")
    @patch("clickhouse_alembic.bootstrap.get_env_config")
    def test_executes_sql_statements(
        self, mock_get_env_config, mock_get_secret, mock_env_config, mock_clickhouse_connect
    ):
        """Test that run_bootstrap executes SQL statements on the client."""
        mock_module, mock_client = mock_clickhouse_connect
        mock_get_env_config.return_value = mock_env_config
        mock_get_secret.side_effect = lambda env, key, **kwargs: {
            "admin_password": "adminpass",
            "migration_password": "migrationpass",
        }.get(key)

        with patch.dict(sys.modules, {"clickhouse_connect": mock_module}):
            run_bootstrap("dev", config_path=Path("/fake/config.yaml"))

        # Verify client was created with correct params
        mock_module.get_client.assert_called_once()
        call_kwargs = mock_module.get_client.call_args[1]
        assert call_kwargs["host"] == "localhost"
        assert call_kwargs["username"] == "default"
        assert call_kwargs["password"] == "adminpass"

        # Verify SQL was executed
        assert mock_client.command.call_count > 0

    @patch("clickhouse_alembic.bootstrap.get_secret")
    @patch("clickhouse_alembic.bootstrap.get_env_config")
    def test_dry_run_does_not_execute(
        self, mock_get_env_config, mock_get_secret, mock_env_config, mock_clickhouse_connect
    ):
        """Test that dry_run=True prints SQL without executing."""
        mock_module, mock_client = mock_clickhouse_connect
        mock_get_env_config.return_value = mock_env_config
        mock_get_secret.side_effect = lambda env, key, **kwargs: {
            "admin_password": "adminpass",
            "migration_password": "migrationpass",
        }.get(key)

        run_bootstrap("dev", config_path=Path("/fake/config.yaml"), dry_run=True)

        # Client should not be created in dry run (import never happens)
        mock_module.get_client.assert_not_called()

    @patch("clickhouse_alembic.bootstrap.get_secret")
    @patch("clickhouse_alembic.bootstrap.get_env_config")
    def test_raises_without_migration_user(self, mock_get_env_config, mock_get_secret):
        """Test that missing migration_user raises ValueError."""
        mock_get_env_config.return_value = {
            "host": "localhost",
            "database": "myproject_dev",
            # No migration_user or user field
        }
        mock_get_secret.side_effect = lambda env, key, **kwargs: "password"

        with pytest.raises(ValueError, match="migration_user is required"):
            run_bootstrap("dev", config_path=Path("/fake/config.yaml"))

    @patch("clickhouse_alembic.bootstrap.get_secret")
    @patch("clickhouse_alembic.bootstrap.get_env_config")
    def test_supports_legacy_user_field(
        self, mock_get_env_config, mock_get_secret, mock_clickhouse_connect
    ):
        """Test that legacy 'user' field is supported."""
        mock_module, mock_client = mock_clickhouse_connect
        mock_get_env_config.return_value = {
            "host": "localhost",
            "database": "myproject_dev",
            "user": "migration_dev",  # Legacy field
            "project": "myproject",
            "secure": False,
        }
        mock_get_secret.side_effect = lambda env, key, **kwargs: "password"

        with patch.dict(sys.modules, {"clickhouse_connect": mock_module}):
            run_bootstrap("dev", config_path=Path("/fake/config.yaml"))

        # Should work without error
        mock_module.get_client.assert_called_once()

    @patch("clickhouse_alembic.bootstrap.get_secret")
    @patch("clickhouse_alembic.bootstrap.get_env_config")
    def test_includes_optional_users_when_configured(
        self, mock_get_env_config, mock_get_secret, mock_clickhouse_connect
    ):
        """Test that dict_reader and mcp_user are included when configured."""
        mock_module, mock_client = mock_clickhouse_connect
        mock_get_env_config.return_value = {
            "host": "localhost",
            "database": "myproject_dev",
            "migration_user": "migration_dev",
            "project": "myproject",
            "dict_reader_name": "dict_reader",
            "mcp_user_name": "mcp_user",
            "secure": False,
        }
        mock_get_secret.side_effect = lambda env, key, **kwargs: f"{key}_value"

        with patch.dict(sys.modules, {"clickhouse_connect": mock_module}):
            run_bootstrap("dev", config_path=Path("/fake/config.yaml"))

        # Get all executed SQL
        executed_sql = " ".join(str(call[0][0]) for call in mock_client.command.call_args_list)

        assert "dict_reader" in executed_sql
        assert "mcp_user" in executed_sql
