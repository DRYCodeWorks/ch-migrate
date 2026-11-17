"""Tests for migration helpers."""

from pathlib import Path
from types import SimpleNamespace

import alembic
import pytest

from ch_migrate.helpers import (
    DICT_READER_PASSWORD_ATTRIBUTE,
    _parse_source_table,
    create_dictionary,
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


DICTIONARY_SQL = (
    "CREATE DICTIONARY IF NOT EXISTS {db}.statuses (id UInt64, status String) PRIMARY KEY id "
    "SOURCE(CLICKHOUSE(TABLE 'events' USER 'dict_reader' PASSWORD '{password}')) "
    "LAYOUT(FLAT()) LIFETIME(0)"
)


class TestCreateDictionary:
    @pytest.fixture
    def executed(self, tmp_path: Path, monkeypatch):
        """Statements create_dictionary sends, in a project whose environment holds a secret."""
        sql_dir = tmp_path / "migrations" / "sql"
        sql_dir.mkdir(parents=True)
        (sql_dir / "dict.sql").write_text(DICTIONARY_SQL)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("CH_DATABASE", "mydb")
        monkeypatch.delenv("DICT_READER_PASSWORD", raising=False)
        statements: list[str] = []
        monkeypatch.setattr(alembic, "op", SimpleNamespace(execute=statements.append))
        attributes = {DICT_READER_PASSWORD_ATTRIBUTE: "from-environment"}
        monkeypatch.setattr(
            alembic, "context", SimpleNamespace(config=SimpleNamespace(attributes=attributes))
        )
        return statements

    def test_grants_then_creates_without_access_yaml(self, executed):
        create_dictionary("dict.sql")
        assert executed[0] == "GRANT SELECT ON mydb.events TO dict_reader"
        assert executed[1].startswith("CREATE DICTIONARY IF NOT EXISTS mydb.statuses")
        assert len(executed) == 2

    def test_only_creates_with_access_yaml(self, executed, tmp_path: Path):
        (tmp_path / "access.yaml").write_text("migration_role:\n  profile: core\n")
        create_dictionary("dict.sql")
        assert len(executed) == 1
        assert executed[0].startswith("CREATE DICTIONARY IF NOT EXISTS mydb.statuses")

    def test_an_empty_access_yaml_is_an_error_not_legacy_mode(self, executed, tmp_path: Path):
        """The access commands' rule, so up can't skip the grant."""
        (tmp_path / "access.yaml").write_text("# nothing yet\n")
        with pytest.raises(ValueError, match="access.yaml is empty"):
            create_dictionary("dict.sql")
        assert executed == []

    def test_reads_the_environment_secret(self, executed):
        create_dictionary("dict.sql")
        assert "PASSWORD 'from-environment'" in executed[-1]

    def test_dict_reader_password_wins_over_the_environment_secret(self, executed, monkeypatch):
        monkeypatch.setenv("DICT_READER_PASSWORD", "from-variable")
        create_dictionary("dict.sql")
        assert "PASSWORD 'from-variable'" in executed[-1]

    def test_an_explicit_password_wins(self, executed, monkeypatch):
        monkeypatch.setenv("DICT_READER_PASSWORD", "from-variable")
        create_dictionary("dict.sql", password="explicit")
        assert "PASSWORD 'explicit'" in executed[-1]

    def test_a_colon_reaches_sqlalchemy_escaped(self, executed):
        """SQLAlchemy reads `:name` as a bind parameter."""
        create_dictionary("dict.sql", password=":lead")
        assert "PASSWORD '\\:lead'" in executed[-1]

    def test_no_password_anywhere_names_where_to_set_it(self, executed, monkeypatch):
        monkeypatch.setattr(
            alembic, "context", SimpleNamespace(config=SimpleNamespace(attributes={}))
        )
        with pytest.raises(ValueError, match="CH_<ENV>_DICT_READER_PASSWORD"):
            create_dictionary("dict.sql")
        assert executed == []
