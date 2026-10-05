"""SQL-first guarded rebuilds and the deprecated EXCHANGE scaffold."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any


def fetch_current_ddl(env_config: dict[str, Any], table_name: str) -> str | None:
    """Fetch current CREATE TABLE statement from the live database.

    Args:
        env_config: Environment config dict from get_env_config().
        table_name: Table name to inspect.

    Returns:
        DDL string or None if connection fails or table doesn't exist.
    """
    from clickhouse_connect.driver.binding import quote_identifier

    from ch_migrate.connection import get_client

    client = None
    try:
        client = get_client(env_config)
        db = env_config["database"]
        result = client.query(
            f"SHOW CREATE TABLE {quote_identifier(db)}.{quote_identifier(table_name)}"
        )
        if result.result_rows:
            return result.result_rows[0][0]
    except Exception:
        return None
    finally:
        if client is not None:
            client.close()
    return None


def find_dependent_dictionaries(
    env_config: dict[str, Any], table_name: str
) -> list[str]:
    """Find dictionaries that use this table as a source.

    Queries system.dictionaries to find any dictionary whose source
    references the given table.

    Args:
        env_config: Environment config dict from get_env_config().
        table_name: Table name to check.

    Returns:
        List of dictionary names that depend on this table.
    """
    from ch_migrate.connection import get_client

    try:
        client = get_client(env_config)
        db = env_config["database"]
        result = client.query(
            "SELECT name FROM system.dictionaries "
            "WHERE database = {db:String} "
            "AND (source LIKE {exact:String} OR source LIKE {dotted:String})",
            parameters={
                "db": db,
                "exact": f"%'{table_name}'%",
                "dotted": f"%.{table_name}%",
            },
        )
        return [row[0] for row in result.result_rows]
    except Exception:
        return []


def generate_rebuild_sql(table_name: str, current_ddl: str | None = None) -> str:
    """Start a portable replacement CREATE, or an explicitly commented offline placeholder."""
    from ch_migrate.rebuild_ddl import _quote

    header = (
        f"-- Guarded online rebuild of {table_name}.\n"
        "-- Edit ORDER BY, the engine or other desired schema details here; keep the table name.\n"
        "-- The rebuild copies and swaps while writers keep running.\n"
        "-- Duplicate copies are possible inside the recorded short rebuild window.\n"
        "-- Reverse this change with another forward rebuild migration.\n"
        "-- {db} is replaced with the selected environment's database.\n\n"
    )
    if current_ddl is None:
        return header + (
            "-- Live DDL was unavailable. Replace these comments with one complete CREATE TABLE.\n"
            f"-- Start from SHOW CREATE TABLE {{db}}.{_quote(table_name)}.\n"
            f"-- CREATE TABLE {{db}}.{_quote(table_name)} (your column definitions)\n"
            "-- ENGINE = your_engine ORDER BY your_sorting_key;\n"
        )
    return header + _portable_rebuild_ddl(current_ddl, table_name).rstrip().rstrip(";") + ";\n"


def _make_shadow_ddl(ddl: str, table_name: str) -> str:
    """Transform a CREATE TABLE statement into a shadow table version.

    Replaces the table name with <table>_shadow and adds IF NOT EXISTS.
    """
    # Replace table name (handles db.table and just table patterns)
    shadow = re.sub(
        rf"(CREATE\s+TABLE\s+)(\S+\.)?{re.escape(table_name)}\b",
        rf"\g<1>\g<2>{table_name}_shadow",
        ddl,
        count=1,
        flags=re.IGNORECASE,
    )
    # Add IF NOT EXISTS if not present
    if "IF NOT EXISTS" not in shadow.upper():
        shadow = re.sub(
            r"(CREATE\s+TABLE\s+)",
            r"\1IF NOT EXISTS ",
            shadow,
            count=1,
            flags=re.IGNORECASE,
        )
    return shadow


def generate_exchange_sql(
    table_name: str, current_ddl: str | None = None
) -> str:
    """Generate the SQL file content for an EXCHANGE TABLES migration.

    This creates the shadow table DDL that the user should modify with
    their desired schema changes before running the migration.

    Args:
        table_name: Name of the table being altered.
        current_ddl: Current CREATE TABLE DDL from the database, if available.

    Returns:
        SQL file content for the shadow table creation.
    """
    if current_ddl:
        # read_sql() runs str.format on this file; keep engine macros such as {uuid} and
        # {replica} (Replicated/Shared engines, ClickHouse Cloud) literal.
        shadow_ddl = _make_shadow_ddl(current_ddl, table_name).replace("{", "{{").replace("}", "}}")
        return (
            f"-- Shadow table for EXCHANGE TABLES migration\n"
            f"-- Modify this schema with your desired changes.\n"
            f"--\n"
            f"-- Original DDL fetched from live database.\n"
            f"-- The migration will:\n"
            f"--   1. Create this shadow table\n"
            f"--   2. Copy data from {table_name} into it\n"
            f"--   3. Atomically swap via EXCHANGE TABLES\n"
            f"--   4. Drop the old table\n\n"
            f"{shadow_ddl}\n"
        )

    # Placeholder when no live DDL is available
    return (
        f"-- Shadow table for EXCHANGE TABLES migration\n"
        f"-- Replace this placeholder with your desired schema.\n"
        f"--\n"
        f"-- TIP: Run `clickhouse-client --query 'SHOW CREATE TABLE {{db}}.{table_name}'`\n"
        f"--      to get the current schema, then modify it here.\n\n"
        f"CREATE TABLE IF NOT EXISTS {{db}}.{table_name}_shadow\n"
        f"(\n"
        f"    -- TODO: Define columns here\n"
        f")\n"
        f"ENGINE = MergeTree\n"
        f"ORDER BY tuple()\n"
    )


def generate_exchange_migration(
    revision: str,
    down_revision: str | None,
    message: str,
    table_name: str,
    sql_path: str,
    dict_names: list[str] | None = None,
) -> str:
    """Generate migration .py content with the EXCHANGE TABLES pattern.

    Args:
        revision: Alembic revision ID.
        down_revision: Previous revision ID.
        message: Migration description.
        table_name: Table being exchanged.
        sql_path: Relative path to the SQL history file (from migrations/sql/).
        dict_names: Dictionaries to reload after exchange, if any.

    Returns:
        Complete migration .py file content.
    """
    down_repr = repr(down_revision)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    dict_lines = ""
    if dict_names:
        dict_lines = "\n    # Reload dependent dictionaries\n"
        for d in dict_names:
            dict_lines += f'    op.execute("SYSTEM RELOAD DICTIONARY {{db}}.{d}")\n'

    return f'''"""{message}

Revision ID: {revision}
Revises: {down_revision or "None"}
Create Date: {now}

EXCHANGE TABLES migration for: {table_name}
Steps: CREATE shadow -> INSERT SELECT -> EXCHANGE -> DROP
"""

from alembic import op

from ch_migrate import IrreversibleMigration, get_db, read_sql

# revision identifiers
revision = {repr(revision)}
down_revision = {down_repr}
branch_labels = None
depends_on = None
irreversible = (
    "EXCHANGE TABLES drops the old table; its data cannot be restored automatically."
)


def upgrade() -> None:
    db = get_db()

    # 1. Create shadow table with new schema
    op.execute(read_sql("{sql_path}", db=db))

    # 2. Copy data from original table into shadow
    #    NOTE: Modify the SELECT if columns changed (added/removed/renamed)
    op.execute(f"INSERT INTO {{db}}.{table_name}_shadow SELECT * FROM {{db}}.{table_name}")

    # 3. Atomically swap tables
    op.execute(f"EXCHANGE TABLES {{db}}.{table_name} AND {{db}}.{table_name}_shadow")

    # 4. Drop old table (now named {table_name}_shadow)
    op.execute(f"DROP TABLE IF EXISTS {{db}}.{table_name}_shadow")
{dict_lines}

def downgrade() -> None:
    raise IrreversibleMigration(revision, irreversible)
'''


def rewrite_migration_file(
    migration_path: Path,
    table_name: str,
    sql_path: str,
    dict_names: list[str] | None = None,
) -> None:
    """Rewrite an alembic-generated migration file with EXCHANGE pattern.

    Reads the revision and down_revision from the existing file, then
    overwrites it with the EXCHANGE TABLES template.

    Args:
        migration_path: Path to the generated migration .py file.
        table_name: Table being exchanged.
        sql_path: Relative path to the SQL history file.
        dict_names: Dictionaries to reload after exchange, if any.
    """
    content = migration_path.read_text()

    rev_match = re.search(r'revision\s*=\s*["\'](\w+)["\']', content)
    down_match = re.search(r'down_revision\s*=\s*["\'](\w+)["\']', content)
    msg_match = re.search(r'^"""(.+?)$', content, re.MULTILINE)

    revision = rev_match.group(1) if rev_match else "UNKNOWN"
    down_revision = down_match.group(1) if down_match else None
    message = msg_match.group(1) if msg_match else table_name

    new_content = generate_exchange_migration(
        revision=revision,
        down_revision=down_revision,
        message=message,
        table_name=table_name,
        sql_path=sql_path,
        dict_names=dict_names,
    )
    migration_path.write_text(new_content)


def _portable_rebuild_ddl(ddl: str, table_name: str) -> str:
    from ch_migrate.rebuild_ddl import _header, _ident, _quote, _replace, _tokens, _upper

    tokens = _tokens(ddl)
    if len(tokens) < 4 or tuple(_upper(token) for token in tokens[:2]) != ("CREATE", "TABLE"):
        raise ValueError("Live DDL is not a CREATE TABLE")
    start = 2
    changes = []
    if tuple(_upper(token) for token in tokens[2:5]) == ("IF", "NOT", "EXISTS"):
        changes.append((tokens[2].start, tokens[4].end, ""))
        start = 5
    end = start + 2 if tokens[start + 1].text == "." else start
    if _ident(tokens[end]) != table_name:
        raise ValueError("Live DDL names a different table")
    _, uuid_span, cluster_span = _header(tokens, end + 1)
    changes.append((tokens[start].start, tokens[end].end, f"{{db}}.{_quote(table_name)}"))
    for span in (uuid_span, cluster_span):
        if span:
            changes.append((*span, ""))
    return _replace(ddl, changes)
