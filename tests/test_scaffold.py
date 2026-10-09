"""Tests for EXCHANGE TABLES scaffold generation."""

from pathlib import Path

import pytest

from ch_migrate.scaffold import (
    _make_shadow_ddl,
    find_dependent_dictionaries,
    generate_exchange_migration,
    generate_exchange_sql,
    rewrite_migration_file,
)


class TestMakeShadowDdl:
    def test_renames_table(self):
        ddl = "CREATE TABLE mydb.users (id UInt64) ENGINE = MergeTree ORDER BY id"
        result = _make_shadow_ddl(ddl, "users")
        assert "mydb.users_shadow" in result
        assert "mydb.users " not in result

    def test_adds_if_not_exists(self):
        ddl = "CREATE TABLE mydb.users (id UInt64) ENGINE = MergeTree ORDER BY id"
        result = _make_shadow_ddl(ddl, "users")
        assert "IF NOT EXISTS" in result

    def test_preserves_existing_if_not_exists(self):
        ddl = "CREATE TABLE IF NOT EXISTS mydb.users (id UInt64) ENGINE = MergeTree ORDER BY id"
        result = _make_shadow_ddl(ddl, "users")
        assert result.count("IF NOT EXISTS") == 1

    def test_handles_no_database_prefix(self):
        ddl = "CREATE TABLE users (id UInt64) ENGINE = MergeTree ORDER BY id"
        result = _make_shadow_ddl(ddl, "users")
        assert "users_shadow" in result


class TestGenerateExchangeSql:
    def test_with_current_ddl(self):
        ddl = "CREATE TABLE mydb.users (id UInt64) ENGINE = MergeTree ORDER BY id"
        result = generate_exchange_sql("users", ddl)
        assert "users_shadow" in result
        assert "Shadow table for EXCHANGE TABLES migration" in result
        assert "Original DDL fetched from live database" in result

    def test_without_current_ddl(self):
        result = generate_exchange_sql("users")
        assert "users_shadow" in result
        assert "TODO: Define columns here" in result
        assert "SHOW CREATE TABLE {db}.users" in result

    def test_placeholder_uses_db_variable(self):
        result = generate_exchange_sql("users")
        assert "{db}.users_shadow" in result

    def test_live_ddl_engine_macros_survive_read_sql_formatting(self):
        ddl = (
            "CREATE TABLE mydb.users (id UInt64) "
            "ENGINE = SharedMergeTree('/clickhouse/tables/{uuid}/{shard}', '{replica}') ORDER BY id"
        )
        rendered = generate_exchange_sql("users", ddl).format(db="mydb")
        assert "SharedMergeTree('/clickhouse/tables/{uuid}/{shard}', '{replica}')" in rendered


class TestGenerateExchangeMigration:
    def test_generates_valid_migration(self):
        content = generate_exchange_migration(
            revision="abc123",
            down_revision="def456",
            message="alter_users",
            table_name="users",
            sql_path="history/tables/users/2024_01_01_0000_abc123.sql",
        )
        assert "revision = 'abc123'" in content
        assert "down_revision = 'def456'" in content
        assert "EXCHANGE TABLES" in content
        assert "users_shadow" in content
        assert "INSERT INTO {db}.users_shadow SELECT * FROM {db}.users" in content
        assert "DROP TABLE IF EXISTS {db}.users_shadow" in content

    def test_includes_dict_reload(self):
        content = generate_exchange_migration(
            revision="abc123",
            down_revision="def456",
            message="alter_users",
            table_name="users",
            sql_path="history/tables/users/001_abc123.sql",
            dict_names=["dict_users", "dict_user_roles"],
        )
        assert "SYSTEM RELOAD DICTIONARY {db}.dict_users" in content
        assert "SYSTEM RELOAD DICTIONARY {db}.dict_user_roles" in content

    def test_no_dict_reload_when_empty(self):
        content = generate_exchange_migration(
            revision="abc123",
            down_revision=None,
            message="alter_users",
            table_name="users",
            sql_path="history/tables/users/001_abc123.sql",
        )
        assert "SYSTEM RELOAD DICTIONARY" not in content

    def test_downgrade_raises(self):
        content = generate_exchange_migration(
            revision="abc123",
            down_revision="def456",
            message="alter_users",
            table_name="users",
            sql_path="history/tables/users/001_abc123.sql",
        )
        from ch_migrate import IrreversibleMigration

        namespace = {}
        exec(compile(content, "<generated migration>", "exec"), namespace)
        with pytest.raises(IrreversibleMigration) as raised:
            namespace["downgrade"]()
        assert raised.value.revision == "abc123"
        assert raised.value.reason == namespace["irreversible"]

    def test_none_down_revision(self):
        content = generate_exchange_migration(
            revision="abc123",
            down_revision=None,
            message="first_migration",
            table_name="users",
            sql_path="history/tables/users/001_abc123.sql",
        )
        assert "down_revision = None" in content


class TestRewriteMigrationFile:
    def test_rewrites_migration(self, tmp_path: Path):
        migration = tmp_path / "001_abc123.py"
        migration.write_text(
            '"""alter_users\n\n'
            "Revision ID: abc123\n"
            "Revises: def456\n"
            '"""\n\n'
            "revision = 'abc123'\n"
            "down_revision = 'def456'\n"
            "\n\ndef upgrade():\n    pass\n\ndef downgrade():\n    pass\n"
        )

        rewrite_migration_file(
            migration,
            table_name="users",
            sql_path="history/tables/users/001_abc123.sql",
        )

        content = migration.read_text()
        assert "EXCHANGE TABLES" in content
        assert "users_shadow" in content
        assert "revision = 'abc123'" in content
        assert "down_revision = 'def456'" in content

    def test_rewrites_with_dict_names(self, tmp_path: Path):
        migration = tmp_path / "001_abc123.py"
        migration.write_text(
            '"""alter_users\n\n"""\n\n' "revision = 'abc123'\n" "down_revision = 'def456'\n"
        )

        rewrite_migration_file(
            migration,
            table_name="users",
            sql_path="history/tables/users/001_abc123.sql",
            dict_names=["dict_users"],
        )

        content = migration.read_text()
        assert "SYSTEM RELOAD DICTIONARY {db}.dict_users" in content


class TestFindDependentDictionaries:
    def test_uses_precise_pattern_matching(self):
        """Search pattern should not match substring (e.g., 'logs' matching 'old_logs')."""
        from unittest.mock import MagicMock, patch

        mock_client = MagicMock()
        mock_client.query.return_value.result_rows = []

        env_config = {
            "database": "default",
            "host": "localhost",
            "user": "default",
            "password": "test",
        }

        with patch("ch_migrate.connection.get_client", return_value=mock_client):
            find_dependent_dictionaries(env_config, "logs")

        call_args = mock_client.query.call_args
        params = call_args[1].get("parameters") or (
            call_args[0][1] if len(call_args[0]) > 1 else {}
        )

        # The pattern should NOT be a bare '%logs%' that matches 'old_logs'
        if "pattern" in params:
            assert (
                params["pattern"] != "%logs%"
            ), "Pattern should use precise matching, not bare substring"
