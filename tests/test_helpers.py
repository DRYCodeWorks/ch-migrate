"""Tests for migration helpers."""

from pathlib import Path

import pytest

from ch_migrate.helpers import (
    _parse_source_table,
    get_cluster,
    get_db,
    on_cluster,
    read_sql,
)


class TestReadSql:
    def test_reads_sql_file(self, tmp_path: Path, monkeypatch):
        sql_dir = tmp_path / "migrations" / "sql"
        sql_dir.mkdir(parents=True)
        sql_file = sql_dir / "test.sql"
        sql_file.write_text("SELECT 1")

        monkeypatch.chdir(tmp_path)

        result = read_sql("test.sql")
        assert result == "SELECT 1"

    def test_substitutes_placeholders(self, tmp_path: Path, monkeypatch):
        sql_dir = tmp_path / "migrations" / "sql"
        sql_dir.mkdir(parents=True)
        sql_file = sql_dir / "test.sql"
        sql_file.write_text("CREATE TABLE {db}.users (id UInt64)")

        monkeypatch.chdir(tmp_path)

        result = read_sql("test.sql", db="mydb")
        assert result == "CREATE TABLE mydb.users (id UInt64)"

    def test_raises_on_missing_file(self, tmp_path: Path, monkeypatch):
        sql_dir = tmp_path / "migrations" / "sql"
        sql_dir.mkdir(parents=True)
        monkeypatch.chdir(tmp_path)

        with pytest.raises(FileNotFoundError, match="SQL file not found"):
            read_sql("nonexistent.sql")


class TestGetDb:
    def test_returns_database_from_env(self, monkeypatch):
        monkeypatch.setenv("CH_DATABASE", "testdb")
        assert get_db() == "testdb"

    def test_returns_default_when_not_set(self, monkeypatch):
        monkeypatch.delenv("CH_DATABASE", raising=False)
        assert get_db() == "default"


class TestGetCluster:
    def test_returns_cluster_from_env(self, monkeypatch):
        monkeypatch.setenv("CH_CLUSTER", "my_cluster")
        assert get_cluster() == "my_cluster"

    def test_returns_none_when_not_set(self, monkeypatch):
        monkeypatch.delenv("CH_CLUSTER", raising=False)
        assert get_cluster() is None

    def test_returns_none_for_empty_string(self, monkeypatch):
        monkeypatch.setenv("CH_CLUSTER", "")
        assert get_cluster() is None


class TestOnCluster:
    def test_returns_on_cluster_clause(self, monkeypatch):
        monkeypatch.setenv("CH_CLUSTER", "my_cluster")
        assert on_cluster() == "ON CLUSTER my_cluster"

    def test_returns_empty_string_when_not_set(self, monkeypatch):
        monkeypatch.delenv("CH_CLUSTER", raising=False)
        assert on_cluster() == ""

    def test_works_in_sql_template(self, tmp_path, monkeypatch):
        sql_dir = tmp_path / "migrations" / "sql"
        sql_dir.mkdir(parents=True)
        sql_file = sql_dir / "test.sql"
        sql_file.write_text("CREATE TABLE {db}.users {on_cluster} (id UInt64)")
        monkeypatch.chdir(tmp_path)

        monkeypatch.setenv("CH_CLUSTER", "default")
        result = read_sql("test.sql", db="mydb", on_cluster=on_cluster())
        assert result == "CREATE TABLE mydb.users ON CLUSTER default (id UInt64)"

    def test_no_cluster_in_sql_template(self, tmp_path, monkeypatch):
        sql_dir = tmp_path / "migrations" / "sql"
        sql_dir.mkdir(parents=True)
        sql_file = sql_dir / "test.sql"
        sql_file.write_text("CREATE TABLE {db}.users {on_cluster} (id UInt64)")
        monkeypatch.chdir(tmp_path)

        monkeypatch.delenv("CH_CLUSTER", raising=False)
        result = read_sql("test.sql", db="mydb", on_cluster=on_cluster())
        assert result == "CREATE TABLE mydb.users  (id UInt64)"


class TestParseSourceTable:
    def test_parses_table_pattern(self):
        sql = """
        CREATE DICTIONARY mydb.dict_users
        (id UInt64, name String)
        PRIMARY KEY id
        SOURCE(CLICKHOUSE(TABLE 'users' DB 'mydb'))
        """
        assert _parse_source_table(sql) == "users"

    def test_parses_query_pattern(self):
        sql = """
        CREATE DICTIONARY mydb.dict_orders
        (id UInt64, total Decimal(10,2))
        PRIMARY KEY id
        SOURCE(CLICKHOUSE(QUERY 'SELECT id, total FROM mydb.orders WHERE active = 1'))
        """
        assert _parse_source_table(sql) == "orders"

    def test_returns_none_when_no_source(self):
        sql = "CREATE DICTIONARY mydb.dict_static (id UInt64) PRIMARY KEY id"
        assert _parse_source_table(sql) is None

    def test_handles_multiline_query(self):
        sql = """
        SOURCE(CLICKHOUSE(QUERY '
            SELECT id, name
            FROM mydb.customers
            WHERE status = 1
        '))
        """
        assert _parse_source_table(sql) == "customers"
