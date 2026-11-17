"""Tests for bootstrap module."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# The stand-in below replaces the `clickhouse_connect` module only; its driver stays real.
import clickhouse_connect.driver.exceptions  # noqa: F401
import pytest
import yaml

from ch_migrate.access.privileges import BUNDLED_LIST
from ch_migrate.access.templates import statement
from ch_migrate.bootstrap import (
    build_bootstrap_sql,
    escape_sql_string,
    run_bootstrap,
    validate_identifier,
)

# What 0.8's `build_bootstrap_sql` returned, recorded before it became a wrapper around the
# `legacy` profile: one statement per line, comments dropped and whitespace collapsed.
RECORDED_0_8 = Path(__file__).parent / "fixtures" / "access" / "build_bootstrap_sql_0_8.json"


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

    @pytest.mark.parametrize("case", sorted(json.loads(RECORDED_0_8.read_text())))
    def test_returns_0_8_statements(self, case):
        """The same statements as 0.8, password escaping included, in any order."""
        recorded = json.loads(RECORDED_0_8.read_text())[case]
        mcp, dict_reader = "mcp" in case, "dict_reader" in case
        sql = build_bootstrap_sql(
            project="shop",
            db="shop_dev",
            migration_user="shop_migrator",
            migration_password="pw'one\\x",
            mcp_user_name="shop_mcp" if mcp else None,
            mcp_password="pw-mcp" if mcp else None,
            dict_reader_name="shop_dict" if dict_reader else None,
            dict_reader_password="pw-dict" if dict_reader else None,
        )
        assert sorted(_statements(sql)) == sorted(recorded)

    def test_creates_roles_and_users_before_any_grant(self):
        """Decision 6: 0.8 created the migration user last, after every grant."""
        sql = build_bootstrap_sql(
            project="myproject",
            db="myproject_dev",
            migration_user="migration_dev",
            migration_password="secret123",
            mcp_user_name="mcp_reader",
            mcp_password="mcppass",
        )
        kinds = [" ".join(statement.split()[:2]) for statement in _statements(sql)]
        # The migration and read-only roles, then the migration and MCP users.
        assert kinds[:5] == ["CREATE DATABASE"] + ["CREATE ROLE"] * 2 + ["CREATE USER"] * 2
        assert all(kind.startswith("GRANT") for kind in kinds[5:])

    def test_rejects_invalid_migration_user(self):
        with pytest.raises(ValueError, match="Invalid migration user"):
            build_bootstrap_sql(
                project="myproject",
                db="myproject_dev",
                migration_user="user with spaces",
                migration_password="secret",
            )


class TestRunBootstrap:
    """run_bootstrap() in legacy mode, against a stand-in for clickhouse_connect.

    The stand-in answers like a 26.3 server holding nothing yet, so bootstrap reads back what
    it granted and then checks, logged in as the migration user (design note, 2.5).
    """

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
        mock_client = _FakeClient()
        mock_module.get_client.return_value = mock_client
        return mock_module, mock_client

    @pytest.fixture(autouse=True)
    def secrets(self, monkeypatch):
        for name in ("ADMIN", "MIGRATION", "DICT_READER", "MCP"):
            monkeypatch.delenv(f"CH_DEV_{name}_PASSWORD", raising=False)
        monkeypatch.setenv("CH_DEV_ADMIN_PASSWORD", "adminpass")
        monkeypatch.setenv("CH_DEV_MIGRATION_PASSWORD", "migrationpass")

    def test_executes_sql_statements(self, tmp_path, mock_env_config, mock_clickhouse_connect):
        """Test that run_bootstrap executes SQL statements on the client."""
        mock_module, mock_client = mock_clickhouse_connect
        config_path = _write_config(tmp_path, mock_env_config)

        with patch.dict(sys.modules, {"clickhouse_connect": mock_module}):
            run_bootstrap("dev", config_path=config_path)

        # The admin logs in first; then the migration user, to check its grants (2.5).
        admin, migration = mock_module.get_client.call_args_list
        assert admin.kwargs["host"] == "localhost"
        assert admin.kwargs["username"] == "default"
        assert admin.kwargs["password"] == "adminpass"
        assert migration.kwargs["host"] == "localhost"
        assert migration.kwargs["username"] == "migration_dev"
        assert migration.kwargs["password"] == "migrationpass"

        # Verify SQL was executed
        assert len(mock_client.commands) > 0

    def test_dry_run_does_not_execute(
        self, tmp_path, mock_env_config, mock_clickhouse_connect, monkeypatch
    ):
        """Test that dry_run=True prints SQL without executing, or reading a secret."""
        mock_module, mock_client = mock_clickhouse_connect
        monkeypatch.delenv("CH_DEV_ADMIN_PASSWORD")
        monkeypatch.delenv("CH_DEV_MIGRATION_PASSWORD")
        config_path = _write_config(tmp_path, mock_env_config)

        with patch.dict(sys.modules, {"clickhouse_connect": mock_module}):
            run_bootstrap("dev", config_path=config_path, dry_run=True)

        # Client should not be created in dry run
        mock_module.get_client.assert_not_called()

    def test_raises_without_migration_user(self, tmp_path):
        """Test that missing migration_user raises ValueError."""
        config_path = _write_config(
            tmp_path,
            {
                "host": "localhost",
                "database": "myproject_dev",
                # No migration_user or user field
            },
        )

        with pytest.raises(ValueError, match="migration_user is required"):
            run_bootstrap("dev", config_path=config_path)

    def test_supports_legacy_user_field(self, tmp_path, mock_clickhouse_connect):
        """Test that legacy 'user' field is supported."""
        mock_module, mock_client = mock_clickhouse_connect
        config_path = _write_config(
            tmp_path,
            {
                "host": "localhost",
                "database": "myproject_dev",
                "user": "migration_dev",  # Legacy field
                "project": "myproject",
                "secure": False,
            },
        )

        with patch.dict(sys.modules, {"clickhouse_connect": mock_module}):
            run_bootstrap("dev", config_path=config_path)

        # Should work without error: the admin, then the legacy field's user
        logins = [call.kwargs["username"] for call in mock_module.get_client.call_args_list]
        assert logins == ["default", "migration_dev"]

    def test_includes_optional_users_when_configured(
        self, tmp_path, mock_clickhouse_connect, monkeypatch
    ):
        """Test that dict_reader and mcp_user are included when configured."""
        mock_module, mock_client = mock_clickhouse_connect
        monkeypatch.setenv("CH_DEV_DICT_READER_PASSWORD", "dict_reader_password_value")
        monkeypatch.setenv("CH_DEV_MCP_PASSWORD", "mcp_password_value")
        config_path = _write_config(
            tmp_path,
            {
                "host": "localhost",
                "database": "myproject_dev",
                "migration_user": "migration_dev",
                "project": "myproject",
                "dict_reader_name": "dict_reader",
                "mcp_user_name": "mcp_user",
                "secure": False,
            },
        )

        with patch.dict(sys.modules, {"clickhouse_connect": mock_module}):
            run_bootstrap("dev", config_path=config_path)

        # Get all executed SQL
        executed_sql = " ".join(mock_client.commands)

        assert "dict_reader" in executed_sql
        assert "mcp_user" in executed_sql


class _FakeClient:
    """A 26.3 server holding no user or role yet, on its own disk; it records each command.

    Every CHECK GRANT answers 1.
    """

    server_version = "26.3.1.1"

    def __init__(self):
        self.commands = []
        self.rows = {
            statement("live", "privileges"): json.loads(BUNDLED_LIST.read_text())["privileges"],
            statement("live", "storage"): [{"type": "local_directory"}],
        }

    def command(self, sql, parameters=None, settings=None):
        self.commands.append(sql)
        return 1 if sql.startswith("CHECK GRANT") else None

    def raw_query(self, sql, parameters=None, settings=None):
        """A statement with a bound password, sent form-encoded in the request body."""
        return self.command(sql, parameters, settings)

    def query(self, sql, parameters=None, settings=None):
        rows = self.rows.get(sql, [])
        return SimpleNamespace(named_results=lambda: iter(rows))

    def close(self):
        pass


def _write_config(tmp_path, environment):
    """config.yaml with one environment, `dev`, as written; `project` goes under `project`."""
    environment = dict(environment)
    config = {"environments": {"dev": environment}}
    if "project" in environment:
        config["project"] = {"name": environment.pop("project")}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config))
    return path


def _statements(sql):
    lines = [line for line in sql.splitlines() if not line.strip().startswith("--")]
    return [" ".join(part.split()) for part in "\n".join(lines).split(";") if part.strip()]
