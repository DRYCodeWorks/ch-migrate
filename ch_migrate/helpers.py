"""Helper functions for ClickHouse migrations."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any


def _get_sql_dir() -> Path:
    """Get the SQL directory relative to current working directory."""
    return Path.cwd() / "migrations" / "sql"


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
    Create a dictionary with automatic SELECT grant for dict_reader.

    This helper:
    1. Reads the dictionary SQL file
    2. Parses the source table from the SQL
    3. Grants SELECT on that table to dict_reader
    4. Creates the dictionary

    Args:
        path: Relative path to dictionary SQL file
        db: Database name (defaults to get_db())
        password: dict_reader password (defaults to DICT_READER_PASSWORD env var)

    Example:
        create_dictionary("history/dictionaries/dict_users/001_abc.sql")
    """
    # Import here to avoid circular imports and allow usage without alembic context
    from alembic import op

    if db is None:
        db = get_db()
    if password is None:
        password = os.environ.get("DICT_READER_PASSWORD")
        if not password:
            raise ValueError(
                "DICT_READER_PASSWORD environment variable is required for dictionaries."
            )

    # Read and format the dictionary SQL
    dict_sql = read_sql(path, db=db, password=password)

    # Parse the source table from the SQL
    source_table = _parse_source_table(dict_sql)

    if source_table:
        # Grant SELECT to dict_reader before creating dictionary
        op.execute(f"GRANT SELECT ON {db}.{source_table} TO dict_reader")

    # Create the dictionary
    op.execute(dict_sql)


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
