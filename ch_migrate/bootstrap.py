"""`ch-migrate bootstrap`: the database and the migration role and user for an environment.

Usage:
    ch-migrate bootstrap <environment>
    ch-migrate bootstrap <environment> --dry-run

It runs on the access engine (`ch_migrate.access.bootstrap`; design note, 2.5):
- with an access.yaml, it adds the role's profile and `migration_role.grants`;
- without one, legacy mode gives the grants 0.8 gave, plus the read-only and dictionary
  roles and users config.yaml names, with every failure reported (Decisions 6 and 13);
- either way, it needs ClickHouse 25.8 or later, and ends with `access check`, logged in
  as the migration user.

Its exit codes are `access apply`'s: 0 when done, 1 when something failed (each failure
named), and 2 on an error before any change. `--dry-run` prints the SQL without connecting
or reading a secret. It is safe to run again.

`run_bootstrap` and `build_bootstrap_sql` keep their 0.8 signatures, because client code may
call them. `build_bootstrap_sql` is deprecated.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Optional

import click

from ch_migrate import ui

# Pattern for valid SQL identifiers (database, user, role names)
_IDENTIFIER_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


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
    except BootstrapFailed as failure:
        sys.exit(failure.exit_code)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)


class BootstrapFailed(Exception):
    """`bootstrap` changed something and then failed; what failed is already printed."""

    def __init__(self, exit_code: int = 1):
        super().__init__(f"bootstrap failed (exit code {exit_code})")
        self.exit_code = exit_code


@click.command("bootstrap")
@click.argument("environment")
@click.option("--dry-run", is_flag=True, help="Show SQL without connecting or reading a secret")
@click.option("--verbose", "-v", is_flag=True, help="Show SQL statements as they execute")
def bootstrap_command(environment: str, dry_run: bool, verbose: bool) -> None:
    """Set up the database, migration role and user for an environment.

    With access.yaml, the migration role gets its profile and `migration_role.grants`;
    `access apply` makes the rest. Without it, bootstrap gives what 0.8 gave: the migration
    role and user, and the optional MCP and dict_reader users. Then it checks the grants,
    logged in as the migration user. Safe to run again.

    Requires admin and migration credentials in .env.local or SSM, but none with --dry-run.
    Exits 0 when done, 1 when something failed (each failure named), and 2 on an error
    before any change.
    """
    try:
        run_bootstrap(environment, dry_run=dry_run, verbose=verbose)
    except BootstrapFailed as failure:
        raise click.exceptions.Exit(failure.exit_code) from None
    except Exception as error:  # noqa: BLE001 - run_bootstrap raises these before any change
        _report_error(error)
        raise click.exceptions.Exit(2) from None


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


def build_bootstrap_sql(  # style-ok: public signature kept for compatibility
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
    """Deprecated: the SQL 0.8's `bootstrap` ran, rendered from the built-in `legacy` profile.

    `ch-migrate bootstrap` doesn't run this text any more; it runs the access engine. This
    stays because client code may call it. It returns 0.8's statements, each password a
    quoted string, with the roles and users created before any grant (Decision 6). An MCP or
    dict reader user is included only with its password, as in 0.8.

    Raises ValueError for a name that isn't a valid identifier.
    """
    from ch_migrate.access.bootstrap import legacy_bootstrap_sql
    from ch_migrate.access.settings import ProjectSettings

    settings = ProjectSettings(
        environment="",
        project=validate_identifier(project, "project name"),
        database=validate_identifier(db, "database name"),
        migration_user=validate_identifier(migration_user, "migration user"),
        dict_reader_name=_optional_name(dict_reader_name, dict_reader_password, "dict reader user"),
        mcp_user_name=_optional_name(mcp_user_name, mcp_password, "MCP user"),
    )
    passwords = {
        "migration_password": migration_password,
        "dict_reader_password": dict_reader_password or "",
        "mcp_password": mcp_password or "",
    }
    return legacy_bootstrap_sql(
        settings, lambda secret: f"'{escape_sql_string(passwords[secret.name])}'"
    )


def run_bootstrap(  # style-ok: public signature kept for compatibility
    env_name: str,
    config_path: Optional[Path] = None,
    dry_run: bool = False,
    verbose: bool = False,
) -> None:
    """Bootstrap one environment, as `ch-migrate bootstrap ENV` does.

    `config_path` defaults to ./config.yaml; access.yaml is read from the same directory.
    Before any change it raises: ValueError for config.yaml, and
    `ch_migrate.access.server.AccessError` for access.yaml, a secret or the server. Once a
    change may have been made, a failure is printed, then raises `BootstrapFailed`.
    """
    from ch_migrate.access.bootstrap import Bootstrap, dry_run_sql, run
    from ch_migrate.access.settings import load_settings

    path = config_path or Path.cwd() / "config.yaml"
    request = Bootstrap(load_settings(env_name, path), path.parent, verbose)
    if dry_run:
        ui.step(f"Dry run for {env_name}: the SQL `bootstrap` would run", stderr=True)
        click.echo(dry_run_sql(request))
        return
    code = run(request)
    if code:
        raise BootstrapFailed(code)  # 1, or 130 when interrupted


def _optional_name(name: Optional[str], password: Optional[str], label: str) -> Optional[str]:
    """An optional user's name, validated; 0.8 creates the user only with its password."""
    if name:
        validate_identifier(name, label)
    return name if name and password else None


def _report_error(error: Exception) -> None:
    """What stopped `bootstrap` before any change: each reason a `Refusal` gives, or one."""
    from ch_migrate.access.server import AccessError, hide_secrets

    messages = getattr(error, "messages", None)
    if messages:
        for message in messages:
            ui.error(hide_secrets(message))
        return
    known = isinstance(error, (ValueError, AccessError))
    reason = str(error) if known else f"{type(error).__name__}: {error}"
    ui.error(f"Bootstrap failed: {hide_secrets(reason)}")


if __name__ == "__main__":
    main()
