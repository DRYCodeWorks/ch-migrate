"""Helper functions for ClickHouse migrations."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

# The Alembic config attribute that carries the environment's dict_reader_password secret
# from the migration environment to create_dictionary.
DICT_READER_PASSWORD_ATTRIBUTE = "ch_migrate_dict_reader_password"


def read_sql(path: str, **kwargs: Any) -> str:
    """
    Read a SQL file and substitute placeholders.

    Args:
        path: Relative path from migrations/sql/ (e.g., "history/tables/users/001_abc.sql")
        **kwargs: Values to substitute (e.g., db="mydb", password="secret")

    Returns:
        SQL string with placeholders replaced

    Raises:
        FileNotFoundError: If the SQL file doesn't exist

    Example:
        >>> read_sql("history/tables/users/001_abc.sql", db="mydb")
        'CREATE TABLE mydb.users ...'
    """
    sql_path = _get_sql_dir() / path
    if not sql_path.exists():
        raise FileNotFoundError(f"SQL file not found: {sql_path}")

    sql = sql_path.read_text()
    return sql.format(**kwargs)


def get_db() -> str:
    """
    Get database name from environment.

    Returns:
        Database name from CH_DATABASE env var, or "default" if not set
    """
    return os.environ.get("CH_DATABASE", "default")


def get_cluster() -> str | None:
    """
    Get the cluster name from environment.

    Returns:
        Cluster name from CH_CLUSTER env var, or None if not set
    """
    return os.environ.get("CH_CLUSTER") or None


def on_cluster() -> str:
    """
    Get the ON CLUSTER clause for use in DDL statements.

    This is an opt-in template variable. Not all DDL supports ON CLUSTER
    equally — dictionary creation, some ALTER operations, and system queries
    have version-dependent ON CLUSTER support. Use this explicitly in
    statements where ON CLUSTER is appropriate.

    Returns:
        "ON CLUSTER cluster_name" if cluster is configured, empty string otherwise

    Example:
        >>> read_sql("tables/users.sql", db=get_db(), on_cluster=on_cluster())
        # In SQL: CREATE TABLE {db}.users {on_cluster} (...)
    """
    cluster = get_cluster()
    if cluster:
        return f"ON CLUSTER {cluster}"
    return ""


def get_config_value(key: str) -> str | None:
    """
    Get a configuration value from environment.

    Args:
        key: Environment variable name

    Returns:
        Value or None if not set
    """
    return os.environ.get(key)


def create_dictionary(path: str, *, db: str | None = None, password: str | None = None) -> None:
    """
    Create a dictionary; without an access.yaml, first grant dict_reader SELECT on its source.

    This helper:
    1. Reads the dictionary SQL file, filling in `{db}` and `{password}`
    2. Without an access.yaml, grants SELECT on the source table to dict_reader
    3. Creates the dictionary

    With an access.yaml, the dictionary's reader and its SELECT are declared there, so the
    helper grants nothing: the core profile can't grant, and `access apply` would revoke a
    grant a migration gave that access.yaml doesn't declare.

    Args:
        path: Relative path to dictionary SQL file
        db: Database name (defaults to get_db())
        password: the reader's password (defaults to DICT_READER_PASSWORD, else the
            environment's dict_reader_password secret: its SSM path in config.yaml, or
            CH_<ENV>_DICT_READER_PASSWORD)

    Example:
        create_dictionary("history/dictionaries/dict_users/001_abc.sql")
    """
    # Import here to avoid circular imports and allow usage without alembic context
    from alembic import op

    from ch_migrate.secrets import remember_secret

    if db is None:
        db = get_db()
    password = password or _dict_reader_password()
    # The statement carries the password as a literal, so an error that quotes it must hide it.
    remember_secret(password)
    dict_sql = read_sql(path, db=db, password=password)
    source_table = _parse_source_table(dict_sql)
    if source_table and not _has_access_file():
        # style-ok: GRANT names identifiers, which ClickHouse can't take as bound parameters
        op.execute(f"GRANT SELECT ON {db}.{source_table} TO dict_reader")
    # SQLAlchemy reads `:name` as a bind parameter, so a password starting with `:` broke the
    # statement. `\:` keeps the colon, as run_sql does.
    op.execute(dict_sql.replace(":", r"\:"))


def _parse_source_table(dict_sql: str) -> str | None:
    """
    Parse the source table name from dictionary SQL.

    Supports two patterns:
    1. TABLE 'table_name' (simple table source)
    2. QUERY '...FROM db.table_name...' (query source)

    Returns:
        Table name or None if not found
    """
    # Try TABLE 'table_name' pattern first
    table_match = re.search(r"TABLE\s+'(\w+)'", dict_sql, re.IGNORECASE)
    if table_match:
        return table_match.group(1)

    # Try QUERY pattern: FROM db.table_name
    query_match = re.search(r"FROM\s+\w+\.(\w+)", dict_sql, re.IGNORECASE)
    if query_match:
        return query_match.group(1)

    return None


def _dict_reader_password() -> str:
    # The migration environment resolves config.yaml's dict_reader_password secret, from SSM
    # or CH_<ENV>_DICT_READER_PASSWORD, and hands it over in memory (alembic_env.run).
    from alembic import context

    password = os.environ.get("DICT_READER_PASSWORD") or context.config.attributes.get(
        DICT_READER_PASSWORD_ATTRIBUTE
    )
    if not password:
        raise ValueError(
            "create_dictionary needs the dictionary reader's password: set "
            "CH_<ENV>_DICT_READER_PASSWORD, an ssm path for dict_reader_password in "
            "config.yaml, or DICT_READER_PASSWORD."
        )
    return password


def _has_access_file() -> bool:
    # access.yaml sits beside config.yaml in the project root, the working directory during
    # a migration, as migrations/sql does for _get_sql_dir. The access commands' rule decides:
    # an empty one is an error, not legacy mode.
    from ch_migrate.access.loader import declares_access

    return declares_access(Path.cwd())


def _get_sql_dir() -> Path:
    """Get the SQL directory relative to current working directory."""
    return Path.cwd() / "migrations" / "sql"
