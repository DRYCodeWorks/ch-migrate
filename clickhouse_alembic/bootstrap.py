"""
Bootstrap script for initializing ClickHouse database and users.

Usage:
    ch-migrate bootstrap <environment>
    ch-migrate bootstrap <environment> --dry-run

This script:
1. Connects using admin credentials
2. Creates the target database (if not exists)
3. Creates roles for migration, read-only, and dict access
4. Creates users and assigns roles
5. Is idempotent - safe to run multiple times
"""

import re
import sys
from pathlib import Path
from typing import Optional

from clickhouse_alembic.config import get_env_config
from clickhouse_alembic.secrets import get_secret

# Pattern for valid SQL identifiers (database, user, role names)
_IDENTIFIER_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def validate_identifier(name: str, label: str) -> str:
    """
    Validate a SQL identifier (database, user, role name).

    Args:
        name: The identifier to validate
        label: Human-readable label for error messages (e.g., "database name")

    Returns:
        The validated identifier (unchanged if valid)

    Raises:
        ValueError: If the identifier contains invalid characters
    """
    if not _IDENTIFIER_PATTERN.match(name):
        raise ValueError(
            f"Invalid {label}: {name!r}. "
            f"Must start with letter/underscore and contain only alphanumeric/underscore."
        )
    return name


def escape_sql_string(s: str) -> str:
    """
    Escape a string for use in SQL single-quoted literals.

    Note: This is for string literals (e.g., passwords) only, not identifiers.
    Use validate_identifier() for database/user/role names.
    """
    return s.replace("\\", "\\\\").replace("'", "''")


def build_bootstrap_sql(
    *,
    project: str,
    db: str,
    migration_user: str,
    migration_password: str,
    dict_reader_name: Optional[str] = None,
    dict_reader_password: Optional[str] = None,
    mcp_user_name: Optional[str] = None,
    mcp_password: Optional[str] = None,
) -> str:
    """
    Build the bootstrap SQL with optional users.

    Args:
        project: Project name (used for role prefixes)
        db: Database name
        migration_user: Migration user name
        migration_password: Migration user password
        dict_reader_name: Optional dict reader user name
        dict_reader_password: Optional dict reader password
        mcp_user_name: Optional MCP user name
        mcp_password: Optional MCP user password

    Returns:
        Complete SQL string for bootstrap

    Raises:
        ValueError: If any identifier contains invalid characters
    """
    # Validate all identifiers to prevent SQL injection
    project = validate_identifier(project, "project name")
    db = validate_identifier(db, "database name")
    migration_user = validate_identifier(migration_user, "migration user")
    if dict_reader_name:
        dict_reader_name = validate_identifier(dict_reader_name, "dict reader user")
    if mcp_user_name:
        mcp_user_name = validate_identifier(mcp_user_name, "MCP user")

    lines = [
        "-- Bootstrap script for ClickHouse database",
        "-- Safe to run multiple times (idempotent)",
        "",
        "-- Create database",
        f"CREATE DATABASE IF NOT EXISTS {db};",
        "",
        "-- =============================================================================",
        "-- ROLES",
        "-- =============================================================================",
        "",
        "-- Migration role: schema changes and data operations",
        "-- (Explicit grants instead of ALL for ClickHouse Cloud compatibility)",
        f"CREATE ROLE IF NOT EXISTS {project}_migration_role;",
        "",
        "-- Schema operations (RENAME is part of ALTER)",
        f"GRANT CREATE TABLE, DROP TABLE, UNDROP TABLE, ALTER ON {db}.* TO {project}_migration_role WITH GRANT OPTION;",
        f"GRANT CREATE VIEW, DROP VIEW ON {db}.* TO {project}_migration_role WITH GRANT OPTION;",
        f"GRANT CREATE DICTIONARY, DROP DICTIONARY ON {db}.* TO {project}_migration_role WITH GRANT OPTION;",
        f"GRANT CREATE FUNCTION, DROP FUNCTION ON *.* TO {project}_migration_role WITH GRANT OPTION;",
        "",
        "-- User/role management (for migrations that create RLS users/roles)",
        "-- CURRENT GRANTS: ClickHouse Cloud's admin cannot grant ALTER/DROP USER on its managed sql-console user",
        f"GRANT CURRENT GRANTS(CREATE USER, ALTER USER, DROP USER ON *.*) TO {project}_migration_role WITH GRANT OPTION;",
        f"GRANT CREATE ROLE, ALTER ROLE, DROP ROLE ON *.* TO {project}_migration_role WITH GRANT OPTION;",
        f"GRANT ROLE ADMIN ON *.* TO {project}_migration_role WITH GRANT OPTION;",
        "",
        "-- Row-level security management",
        f"GRANT ACCESS MANAGEMENT ON {db}.* TO {project}_migration_role WITH GRANT OPTION;",
        f"GRANT CREATE ROW POLICY, ALTER ROW POLICY, DROP ROW POLICY, SHOW ROW POLICIES ON {db}.* TO {project}_migration_role WITH GRANT OPTION;",
        "",
        "-- Data operations",
        f"GRANT SELECT, INSERT, DELETE, TRUNCATE, OPTIMIZE ON {db}.* TO {project}_migration_role WITH GRANT OPTION;",
        "",
        "-- Introspection (needed by Alembic)",
        f"GRANT SHOW TABLES, SHOW COLUMNS, SHOW DICTIONARIES ON {db}.* TO {project}_migration_role WITH GRANT OPTION;",
        "",
        "-- Temp tables (for zero-downtime migrations with EXCHANGE TABLES)",
        "-- Note: EXCHANGE TABLES requires DROP on both tables, already granted above",
        f"GRANT CREATE TEMPORARY TABLE ON *.* TO {project}_migration_role WITH GRANT OPTION;",
        "",
        "-- System table access (monitoring migrations, RLS introspection, user/role management)",
        "-- Use CURRENT GRANTS to avoid failure when admin lacks access to some system tables",
        "-- (e.g., system.zookeeper on ClickHouse Cloud)",
        f"GRANT CURRENT GRANTS(SELECT ON system.*) TO {project}_migration_role WITH GRANT OPTION;",
        "-- ClickHouse Cloud requires explicit grants for individual system tables",
        f"GRANT SELECT ON system.grants TO {project}_migration_role;",
    ]

    # MCP readonly role (optional)
    if mcp_user_name and mcp_password:
        lines.extend(
            [
                "",
                "-- Read-only role: for MCP tools",
                f"CREATE ROLE IF NOT EXISTS {project}_readonly_role;",
                f"GRANT SELECT ON {db}.* TO {project}_readonly_role;",
                f"GRANT SHOW TABLES ON {db}.* TO {project}_readonly_role;",
            ]
        )

    # Dict reader role (optional)
    if dict_reader_name and dict_reader_password:
        lines.extend(
            [
                "",
                "-- Dict reader role: for dictionary sources",
                f"CREATE ROLE IF NOT EXISTS {project}_dict_role;",
                "-- SELECT grants added per-table when dictionaries are created",
            ]
        )

    lines.extend(
        [
            "",
            "-- =============================================================================",
            "-- USERS",
            "-- =============================================================================",
            "",
            "-- Migration user (required)",
            f"CREATE USER IF NOT EXISTS {migration_user}",
            f"IDENTIFIED BY '{escape_sql_string(migration_password)}';",
            f"GRANT {project}_migration_role TO {migration_user};",
        ]
    )

    # MCP user (optional)
    if mcp_user_name and mcp_password:
        lines.extend(
            [
                "",
                "-- MCP user: read-only access for AI tools",
                f"CREATE USER IF NOT EXISTS {mcp_user_name}",
                f"IDENTIFIED BY '{escape_sql_string(mcp_password)}';",
                f"GRANT {project}_readonly_role TO {mcp_user_name};",
            ]
        )

    # Dict reader user (optional)
    if dict_reader_name and dict_reader_password:
        lines.extend(
            [
                "",
                "-- Dict reader user: for dictionary sources",
                f"CREATE USER IF NOT EXISTS {dict_reader_name}",
                f"IDENTIFIED BY '{escape_sql_string(dict_reader_password)}';",
                f"GRANT {project}_dict_role TO {dict_reader_name};",
            ]
        )

    return "\n".join(lines)


def run_bootstrap(
    env_name: str,
    config_path: Optional[Path] = None,
    dry_run: bool = False,
    verbose: bool = False,
) -> None:
    """
    Run bootstrap for the specified environment.

    Args:
        env_name: Environment name (dev, staging, production, etc.)
        config_path: Path to config.yaml (defaults to ./config.yaml)
        dry_run: If True, print SQL without executing
        verbose: If True, print each SQL statement as it executes
    """
    if config_path is None:
        config_path = Path.cwd() / "config.yaml"

    print(f"==> Loading environment: {env_name}")
    env_config = get_env_config(env_name, config_path)

    # Get SSM paths if configured
    ssm_config = env_config.get("ssm", {})

    # Get AWS region for SSM lookups (optional, uses AWS default if not set)
    aws_region = env_config.get("aws_region")

    # Get required credentials
    admin_password = get_secret(
        env_name,
        "admin_password",
        ssm_path=ssm_config.get("admin_password"),
        aws_region=aws_region,
        required=True,
    )

    migration_password = get_secret(
        env_name,
        "migration_password",
        ssm_path=ssm_config.get("migration_password"),
        aws_region=aws_region,
        required=True,
    )
    assert admin_password is not None  # required=True ensures this
    assert migration_password is not None  # required=True ensures this

    # Get optional credentials
    dict_reader_name = env_config.get("dict_reader_name")
    dict_reader_password = None
    if dict_reader_name:
        dict_reader_password = get_secret(
            env_name,
            "dict_reader_password",
            ssm_path=ssm_config.get("dict_reader_password"),
            aws_region=aws_region,
            required=True,
        )

    mcp_user_name = env_config.get("mcp_user_name")
    mcp_password = None
    if mcp_user_name:
        mcp_password = get_secret(
            env_name,
            "mcp_password",
            ssm_path=ssm_config.get("mcp_password"),
            aws_region=aws_region,
            required=True,
        )

    # Get migration user - support both new 'migration_user' and legacy 'user' field
    migration_user = env_config.get("migration_user") or env_config.get("user")
    if not migration_user:
        raise ValueError(f"migration_user is required in config.yaml for environment '{env_name}'")

    # Build SQL
    project_config = env_config.get("project")
    if project_config:
        project = project_config
    else:
        # Derive from database name (e.g., myproject_dev -> myproject)
        project = env_config["database"].split("_")[0]

    sql = build_bootstrap_sql(
        project=project,
        db=env_config["database"],
        migration_user=migration_user,
        migration_password=migration_password,
        dict_reader_name=dict_reader_name,
        dict_reader_password=dict_reader_password,
        mcp_user_name=mcp_user_name,
        mcp_password=mcp_password,
    )

    if dry_run:
        print("==> Dry run - SQL that would be executed:")
        print("-" * 60)
        # Mask passwords in output
        masked_sql = sql
        for pw in [migration_password, dict_reader_password, mcp_password]:
            if pw:
                masked_sql = masked_sql.replace(pw, "********")
        print(masked_sql)
        print("-" * 60)
        return

    host = env_config["host"]
    admin_user = env_config.get("admin_user", "default")
    secure = env_config.get("secure", True)
    port = env_config.get("port", 8443 if secure else 8123)

    print(f"==> Connecting as admin: {admin_user}@{host}")
    print(f"==> Target database: {env_config['database']}")
    print(f"==> Migration user: {migration_user}")
    if dict_reader_name:
        print(f"==> Dict reader: {dict_reader_name}")
    if mcp_user_name:
        print(f"==> MCP user: {mcp_user_name}")

    import clickhouse_connect

    client = clickhouse_connect.get_client(
        host=host,
        port=port,
        username=admin_user,
        password=admin_password,
        secure=secure,
        interface="https" if secure else "http",
    )

    # Collect passwords for masking in verbose output
    passwords_to_mask = [p for p in [migration_password, dict_reader_password, mcp_password] if p]

    # Execute each statement
    for statement in sql.split(";"):
        statement = statement.strip()
        # Strip leading comment lines to get to the actual SQL
        lines = statement.split("\n")
        sql_lines = [line for line in lines if not line.strip().startswith("--")]
        actual_sql = "\n".join(sql_lines).strip()
        if actual_sql:
            if verbose:
                # Mask passwords in output
                masked = statement
                for pw in passwords_to_mask:
                    masked = masked.replace(pw, "********")
                print(f"  {masked};")
            client.command(actual_sql)

    print("==> Bootstrap complete!")
    print(f"\nYou can now run migrations:")
    print(f"  ch-migrate up {env_name}")


def main() -> None:
    """CLI entrypoint for bootstrap."""
    if len(sys.argv) < 2:
        print("Usage: ch-migrate bootstrap <environment> [--dry-run]")
        print("Example: ch-migrate bootstrap dev")
        sys.exit(1)

    dry_run = "--dry-run" in sys.argv
    env_name = sys.argv[1]

    try:
        run_bootstrap(env_name, dry_run=dry_run)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
