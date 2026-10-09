"""Command-line interface for clickhouse-alembic."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import click
from dotenv import load_dotenv

from clickhouse_alembic.authoring import NewOptions
from clickhouse_alembic.config import get_env_config

# Load .env.local if it exists in the current directory
_env_local = Path.cwd() / ".env.local"
if _env_local.exists():
    load_dotenv(_env_local)


def get_template_path(name: str) -> Path:
    """Get path to a template file."""
    return Path(__file__).parent / "templates" / "project" / f"{name}.template"


def render_template(template_path: Path, **kwargs: str) -> str:
    """Render a template with substitutions."""
    content = template_path.read_text()
    for key, value in kwargs.items():
        content = content.replace(f"{{{key}}}", value)
    return content


def _run_alembic(
    environment: str, args: list[str], *, exit_on_complete: bool = True
) -> subprocess.CompletedProcess[str] | None:
    """Run alembic with environment configuration.

    Args:
        environment: Environment name from config.yaml
        args: Arguments to pass to alembic
        exit_on_complete: If True, exit after running. If False, return the result.

    Returns:
        CompletedProcess if exit_on_complete=False, otherwise exits.
    """
    config_path = Path.cwd() / "config.yaml"

    try:
        env_config = get_env_config(environment, config_path)
    except Exception as e:
        click.echo(f"Error loading config: {e}", err=True)
        if exit_on_complete:
            sys.exit(1)
        return None

    # Set environment variables for alembic
    env = os.environ.copy()
    env["CH_ENVIRONMENT"] = environment
    env["CH_DATABASE"] = env_config["database"]
    env["CH_HOST"] = env_config["host"]
    env["CH_PORT"] = str(env_config.get("port", 8443))
    env["CH_USER"] = env_config.get("migration_user") or env_config.get("user", "")
    env["CH_PASSWORD"] = env_config.get("password", "")
    env["CH_SECURE"] = "1" if env_config.get("secure", True) else "0"

    # Use sys.executable to run alembic from the same Python environment
    # as ch-migrate, avoiding issues with pyenv shims intercepting the call
    result = subprocess.run(
        [sys.executable, "-m", "alembic"] + args,
        env=env,
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
    )

    # Show output from alembic
    if result.stdout:
        click.echo(result.stdout)
    if result.stderr:
        click.echo(result.stderr, err=True)

    if result.returncode != 0:
        click.echo(f"Alembic command failed with exit code {result.returncode}", err=True)

    if exit_on_complete:
        sys.exit(result.returncode)

    return result


def _refuse_irreversible_downgrade(environment: str, target: str) -> None:
    from clickhouse_alembic.connection import get_current_heads
    from clickhouse_alembic.downgrade import irreversible_reason, revisions_to_revert
    from clickhouse_alembic.rebase import build_revision_graph

    try:
        env_config = get_env_config(environment, Path.cwd() / "config.yaml")
        heads = get_current_heads(env_config)
    except Exception:
        return  # Alembic reports configuration and connection errors itself.
    graph = build_revision_graph(Path.cwd() / "migrations" / "versions")
    revisions = revisions_to_revert(graph, heads, target)
    if revisions is None:
        click.echo("Note: downgrade range is unknown; relying on migration backstops.", err=True)
        return
    irreversible = [(rev, irreversible_reason(graph, rev)) for rev in revisions]
    irreversible = [(rev, reason) for rev, reason in irreversible if reason is not None]
    if not irreversible:
        return
    click.echo("Downgrade refused; nothing was run. Irreversible migrations:", err=True)
    for rev, reason in irreversible:
        click.echo(f"  {rev}: {reason}", err=True)
    click.echo(
        "To revert past these revisions, write their downgrades and remove the "
        "irreversible markers in a reviewed change.",
        err=True,
    )
    sys.exit(1)


@click.group()
@click.version_option()
def main() -> None:
    """ClickHouse migration tool built on Alembic.

    ch-migrate provides a unified CLI for managing ClickHouse database migrations.
    It handles project initialization, database bootstrapping, and running migrations.

    \b
    Quick start:
      ch-migrate init                    # Initialize a new project
      ch-migrate bootstrap dev           # Set up database and users
      ch-migrate up dev                  # Apply pending migrations
      ch-migrate status dev              # Check migration status
    """
    pass


@main.command()
@click.argument("path", default=".", type=click.Path())
@click.option("--name", "-n", default=None, help="Project name (defaults to directory name)")
def init(path: str, name: str | None) -> None:
    """Initialize a new ClickHouse migration project.

    Creates the project structure with config.yaml and migrations directory.
    """
    project_path = Path(path).resolve()

    if name is None:
        name = project_path.name

    # Normalize project name (replace spaces/hyphens with underscores for database names)
    safe_name = name.replace("-", "_").replace(" ", "_").lower()

    click.echo(f"Initializing ClickHouse migration project: {name}")
    click.echo(f"  Path: {project_path}")

    # Create directories
    project_path.mkdir(parents=True, exist_ok=True)
    (project_path / "migrations" / "sql" / "bootstrap").mkdir(parents=True, exist_ok=True)
    (project_path / "migrations" / "sql" / "history" / "tables").mkdir(parents=True, exist_ok=True)
    (project_path / "migrations" / "sql" / "history" / "views").mkdir(parents=True, exist_ok=True)
    (project_path / "migrations" / "sql" / "history" / "dictionaries").mkdir(
        parents=True, exist_ok=True
    )
    (project_path / "migrations" / "versions").mkdir(parents=True, exist_ok=True)

    # Copy/render templates
    templates = [
        ("alembic.ini", "alembic.ini"),
        ("config.yaml", "config.yaml"),
        ("env.local.example", ".env.local.example"),
        ("script.py.mako", "migrations/script.py.mako"),
    ]

    for template_name, output_name in templates:
        template_path = get_template_path(template_name)
        output_path = project_path / output_name

        if output_path.exists():
            click.echo(f"  Skipping {output_name} (already exists)")
            continue

        content = render_template(template_path, project_name=safe_name)
        output_path.write_text(content)
        click.echo(f"  Created {output_name}")

    # Copy env.py from package
    env_py_src = Path(__file__).parent / "env.py"
    env_py_dst = project_path / "migrations" / "env.py"
    if not env_py_dst.exists():
        shutil.copy(env_py_src, env_py_dst)
        click.echo("  Created migrations/env.py")

    # Create .gitignore
    gitignore_path = project_path / ".gitignore"
    if not gitignore_path.exists():
        gitignore_path.write_text(".env.local\n__pycache__/\n*.pyc\n")
        click.echo("  Created .gitignore")

    click.echo("")
    click.echo("Project initialized! Next steps:")
    click.echo("")
    click.echo("  1. Edit config.yaml with your ClickHouse hosts")
    click.echo("  2. Copy .env.local.example to .env.local and add passwords")
    click.echo("  3. Run: ch-migrate bootstrap dev")
    click.echo("  4. Create your first migration: ch-migrate new dev create_users --table users")
    click.echo("     then write its SQL in the .up.sql and .down.sql files it creates")


@main.command()
@click.argument("environment")
@click.option("--dry-run", is_flag=True, help="Show SQL without executing")
@click.option("--verbose", "-v", is_flag=True, help="Show SQL statements as they execute")
def bootstrap(environment: str, dry_run: bool, verbose: bool) -> None:
    """Initialize database and users for an environment.

    Creates the database, roles, and users (migration user, optional MCP user,
    optional dict_reader user). Safe to run multiple times (idempotent).

    Requires admin credentials in .env.local or SSM.
    """
    from clickhouse_alembic.bootstrap import run_bootstrap

    try:
        run_bootstrap(environment, dry_run=dry_run, verbose=verbose)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@main.command()
@click.argument("environment")
@click.option("--revision", "-r", default="head", help="Revision to upgrade to (default: head)")
@click.option(
    "--skip-mv-check",
    is_flag=True,
    help="Skip materialized view declaration validation",
)
def up(environment: str, revision: str, skip_mv_check: bool) -> None:
    """Apply pending migrations.

    Runs all unapplied migrations to bring the database to the latest version.
    Use --revision to upgrade to a specific revision instead of head.

    Validates that migrations creating MATERIALIZED VIEWs include proper
    MV_DECLARATIONS and companion grants. Use --skip-mv-check to bypass.
    """
    if not skip_mv_check:
        versions_dir = Path.cwd() / "migrations" / "versions"
        if versions_dir.exists():
            from clickhouse_alembic.config import load_config
            from clickhouse_alembic.lint import LintConfig
            from clickhouse_alembic.mv_validate import validate_mv_migrations

            cutoff = None
            config_path = Path.cwd() / "config.yaml"
            if config_path.exists():
                try:
                    raw_config = load_config(config_path)
                    lint_config = LintConfig.from_config(raw_config)
                    cutoff = lint_config.mv_validation_cutoff
                except Exception as e:
                    click.echo(
                        f"Warning: Could not load lint config: {e}",
                        err=True,
                    )

            mv_errors = validate_mv_migrations(versions_dir, cutoff_date=cutoff)
            if mv_errors:
                click.echo(
                    click.style(
                        "MV declaration validation failed:", fg="red", bold=True
                    ),
                    err=True,
                )
                for error in mv_errors:
                    prefix = f"  [{error.file}]"
                    if error.mv_name:
                        prefix += f" ({error.mv_name})"
                    click.echo(
                        click.style(f"{prefix}: {error.message}", fg="red"),
                        err=True,
                    )
                click.echo(err=True)
                click.echo(
                    "Fix the issues above, or use --skip-mv-check to bypass.",
                    err=True,
                )
                sys.exit(1)

    _run_alembic(environment, ["upgrade", revision])


@main.command()
@click.argument("environment")
@click.option("--revision", "-r", default="-1", help="Revision to downgrade to (default: -1)")
def down(environment: str, revision: str) -> None:
    """Rollback migrations.

    By default, rolls back the last migration. Use --revision to specify a target.
    """
    _refuse_irreversible_downgrade(environment, revision)
    _run_alembic(environment, ["downgrade", revision])


@main.command()
@click.argument("environment")
def status(environment: str) -> None:
    """Show migration status.

    Displays environment info, applied/pending counts, and head status.
    """
    from clickhouse_alembic.connection import get_current_heads
    from clickhouse_alembic.display import render_status
    from clickhouse_alembic.rebase import build_revision_graph

    config_path = Path.cwd() / "config.yaml"
    try:
        env_config = get_env_config(environment, config_path)
    except Exception as e:
        click.echo(f"Error loading config: {e}", err=True)
        sys.exit(1)

    versions_dir = Path.cwd() / "migrations" / "versions"
    if not versions_dir.exists():
        click.echo("Error: migrations/versions/ not found", err=True)
        sys.exit(1)

    graph = build_revision_graph(versions_dir)

    db_error = None
    applied: set[str] | None = None
    try:
        db_heads = get_current_heads(env_config)
        applied = set()
        for head in db_heads:
            if head in graph.migrations:
                applied.update(graph.walk_to_root(head))
            else:
                click.echo(
                    f"Warning: DB head {head[:12]} not found in local migration files",
                    err=True,
                )
    except Exception as e:
        db_error = str(e)

    render_status(environment, env_config, graph, applied, db_error=db_error)


@main.command()
@click.argument("environment")
def history(environment: str) -> None:
    """Show migration history.

    Displays a tree of all migrations, color-coded by applied status.
    """
    from clickhouse_alembic.connection import get_current_heads
    from clickhouse_alembic.display import render_history
    from clickhouse_alembic.rebase import build_revision_graph

    config_path = Path.cwd() / "config.yaml"
    try:
        env_config = get_env_config(environment, config_path)
    except Exception as e:
        click.echo(f"Error loading config: {e}", err=True)
        sys.exit(1)

    versions_dir = Path.cwd() / "migrations" / "versions"
    if not versions_dir.exists():
        click.echo("Error: migrations/versions/ not found", err=True)
        sys.exit(1)

    graph = build_revision_graph(versions_dir)

    db_error = None
    applied: set[str] | None = None
    try:
        db_heads = get_current_heads(env_config)
        applied = set()
        unresolved_heads: list[str] = []
        for head in db_heads:
            if head in graph.migrations:
                applied.update(graph.walk_to_root(head))
            else:
                unresolved_heads.append(head)

        if unresolved_heads:
            for head in unresolved_heads:
                click.echo(
                    f"Warning: DB head {head[:12]} not found in local migration files",
                    err=True,
                )
            click.echo(
                "Applied status may be incomplete — local files may be out of sync with the database.",
                err=True,
            )
            click.echo()
    except Exception as e:
        db_error = str(e)

    render_history(graph, applied, db_error=db_error)


@main.command()
@click.argument("environment")
@click.argument("name")
@click.option("--table", "-t", "table_name", help="Create SQL file for table (e.g., --table users)")
@click.option(
    "--view", "-v", "view_name", help="Create SQL file for view (e.g., --view active_users)"
)
@click.option(
    "--dict", "-d", "dict_name", help="Create SQL file for dictionary (e.g., --dict regions)"
)
@click.option(
    "--exchange", is_flag=True, help="Generate EXCHANGE TABLES scaffold (requires --table)"
)
@click.option(
    "--python", "python_migration", is_flag=True, help="Keep the Python migration template"
)
@click.option(
    "--irreversible",
    "irreversible_reason",
    metavar="REASON",
    help="Write only upgrade SQL and refuse downgrades, with this reason",
)
def new(
    environment: str,
    name: str,
    table_name: str | None,
    view_name: str | None,
    dict_name: str | None,
    exchange: bool,
    python_migration: bool,
    irreversible_reason: str | None,
) -> None:
    """Create upgrade and downgrade SQL files, plus the revision that runs them.

    Name an object with --table, --view or --dict to group its SQL history.
    Use --irreversible REASON when a change cannot restore dropped data.
    --python keeps the Python template; --exchange still requires --table.
    """
    options = NewOptions(
        table_name, view_name, dict_name, exchange, python_migration, irreversible_reason
    )
    _check_new_options(options)
    result = _run_alembic(environment, ["revision", "-m", name], exit_on_complete=False)
    if result is None or result.returncode != 0:
        sys.exit(1 if result is None else result.returncode)
    migration_path = _find_migration_file(result.stdout)
    if migration_path is None:
        click.echo("Error: could not find the revision file Alembic generated", err=True)
        sys.exit(1)
    if exchange:
        revision = _extract_revision_from_output(result.stdout) or ""
        _create_exchange_scaffold(environment, table_name or "", revision, result.stdout)
    elif python_migration:
        _create_python_migration(migration_path, options)
    else:
        _create_sql_first_migration(migration_path, options)


def _check_new_options(options: NewOptions) -> None:
    problems = []
    if len(options.named_objects()) > 1:
        problems.append("use only one of --table, --view and --dict")
    if options.exchange and not options.table_name:
        problems.append("--exchange requires --table")
    if options.exchange and options.python_migration:
        problems.append("--exchange cannot be combined with --python")
    if options.irreversible_reason is not None:
        if options.exchange or options.python_migration:
            problems.append("--irreversible cannot be combined with --python or --exchange")
        if not options.irreversible_reason.strip():
            problems.append("--irreversible needs a non-empty reason")
    if problems:
        for problem in problems:
            click.echo(f"Error: {problem}", err=True)
        sys.exit(1)


def _create_sql_first_migration(migration_path: Path, options: NewOptions) -> None:
    from clickhouse_alembic.authoring import read_revision_header, render_revision, write_sql_files

    header = read_revision_header(migration_path)
    files = write_sql_files(Path.cwd() / "migrations" / "sql", header, options)
    migration_path.write_text(render_revision(header, files, options.irreversible_reason))
    click.echo(f"  Created migrations/sql/{files.upgrade}")
    if files.downgrade:
        click.echo(f"  Created migrations/sql/{files.downgrade}")
    else:
        click.echo("  Marked irreversible: `ch-migrate down` will refuse to revert it")
    click.echo("  Write the SQL in these files; the revision needs no edits.")


def _create_python_migration(migration_path: Path, options: NewOptions) -> None:
    from clickhouse_alembic.authoring import read_revision_header

    named = options.named_objects()
    if named:
        object_type, object_name = named[0]
        revision = read_revision_header(migration_path).revision
        sql_path = _create_sql_file(object_name, object_type, revision)
        if sql_path:
            click.echo(f"  Created {sql_path.relative_to(Path.cwd())}")


def _extract_revision_from_output(stdout: str) -> str | None:
    """Extract revision ID from alembic output by reading the generated file."""
    # Find the generated file path from output
    # Format: "Generating /path/to/migrations/versions/<filename>.py ...  done"
    # Note: Terminal wrapping may insert newlines/spaces in path
    match = re.search(r"Generating (.+?\.py)", stdout, re.DOTALL)
    if not match:
        return None

    # Clean up newline+spaces inserted by terminal wrapping (preserves intentional spaces)
    file_path = re.sub(r"\n\s*", "", match.group(1))
    migration_file = Path(file_path)
    if not migration_file.exists():
        return None

    # Parse revision from file content
    content = migration_file.read_text()
    rev_match = re.search(r'revision = ["\'](\w+)["\']', content)
    return rev_match.group(1) if rev_match else None


def _create_sql_file(name: str, object_type: str, revision: str) -> Path | None:
    """Create SQL history file for a migration.

    Args:
        name: Object name (e.g., "users")
        object_type: One of "table", "view", "dictionary"
        revision: Alembic revision ID

    Returns:
        Path to created file, or None if failed
    """
    # Determine directory (tables, views, dictionaries)
    type_dir = f"{object_type}s" if object_type != "dictionary" else "dictionaries"
    sql_dir = Path.cwd() / "migrations" / "sql" / "history" / type_dir / name
    sql_dir.mkdir(parents=True, exist_ok=True)

    # Use datetime prefix for ordering (matches alembic's file_template format)
    now = datetime.now()
    date_prefix = now.strftime("%Y_%m_%d_%H%M")

    # Create SQL file with minimal header
    sql_file = sql_dir / f"{date_prefix}_{revision}.sql"
    template = f"""-- {name} {object_type}
-- Migration: {revision}
-- Created: {now.strftime("%Y-%m-%d %H:%M")}

"""
    sql_file.write_text(template)
    return sql_file


def _create_exchange_scaffold(
    environment: str, table_name: str, revision: str, alembic_stdout: str
) -> None:
    """Create EXCHANGE TABLES migration scaffold.

    Rewrites the alembic-generated migration with the EXCHANGE pattern
    and creates a SQL history file for the shadow table.
    """
    from clickhouse_alembic.scaffold import (
        fetch_current_ddl,
        find_dependent_dictionaries,
        generate_exchange_sql,
        rewrite_migration_file,
    )

    config_path = Path.cwd() / "config.yaml"

    # Try to connect to live DB for current DDL and dict detection
    current_ddl: str | None = None
    dict_names: list[str] = []
    try:
        env_config = get_env_config(environment, config_path)
        current_ddl = fetch_current_ddl(env_config, table_name)
        if current_ddl:
            click.echo(f"  Fetched current DDL for {table_name}")
        dict_names = find_dependent_dictionaries(env_config, table_name)
        if dict_names:
            click.echo(f"  Detected dependent dictionaries: {', '.join(dict_names)}")
    except Exception:
        click.echo("  Note: Could not connect to DB; using placeholder DDL", err=True)

    # Create SQL history file with shadow table DDL
    sql_content = generate_exchange_sql(table_name, current_ddl)
    sql_path = _create_sql_file(table_name, "table", revision)
    if sql_path:
        sql_path.write_text(sql_content)
        click.echo(f"  Created {sql_path.relative_to(Path.cwd())}")

        # Rewrite the migration .py with EXCHANGE pattern
        migration_path = _find_migration_file(alembic_stdout)
        if migration_path:
            rel_sql = str(sql_path.relative_to(Path.cwd() / "migrations" / "sql"))
            rewrite_migration_file(migration_path, table_name, rel_sql, dict_names or None)
            click.echo(f"  Rewrote {migration_path.name} with EXCHANGE TABLES pattern")
        else:
            click.echo("Warning: Could not locate migration file to rewrite", err=True)


def _find_migration_file(alembic_stdout: str) -> Path | None:
    """Find the migration .py file path from alembic output."""
    match = re.search(r"Generating (.+?\.py)", alembic_stdout, re.DOTALL)
    if not match:
        return None
    file_path = re.sub(r"\n\s*", "", match.group(1))
    path = Path(file_path)
    return path if path.exists() else None


@main.command()
@click.argument("environment")
@click.option("--onto", default=None, help="Target revision to rebase onto (skips auto-detection)")
@click.option("--dry-run", is_flag=True, help="Show planned changes without applying")
def rebase(environment: str, onto: str | None, dry_run: bool) -> None:
    """Rebase dangling migration branches onto the deployed head.

    Finds migrations that branch off an older revision and rewrites them
    to branch off the current deployed head instead.

    \b
    Guided mode (detects deployed head automatically):
      ch-migrate rebase dev

    \b
    Explicit mode (specify target revision):
      ch-migrate rebase dev --onto abc123
    """
    from clickhouse_alembic.rebase import apply_rebase, plan_rebase

    versions_dir = Path.cwd() / "migrations" / "versions"
    if not versions_dir.exists():
        click.echo("Error: migrations/versions/ not found", err=True)
        sys.exit(1)

    # Check for uncommitted changes to migration files
    result = subprocess.run(
        ["git", "diff", "--name-only", "migrations/versions/"],
        capture_output=True,
        text=True,
        cwd=Path.cwd(),
    )
    if result.returncode == 0 and result.stdout.strip():
        click.echo("Error: uncommitted changes in migration files. Commit or stash first.", err=True)
        sys.exit(1)

    # Determine the target revision
    if onto is None:
        alembic_result = _run_alembic(
            environment, ["current"], exit_on_complete=False
        )
        if alembic_result is None or alembic_result.returncode != 0:
            click.echo(
                "Error: could not determine current revision. "
                "Use --onto to specify the target revision explicitly.",
                err=True,
            )
            sys.exit(1)

        # Parse current revision from alembic output
        # Format: "abc123 (head)" or "abc123"
        current_match = re.search(r"(\w{4,})", alembic_result.stdout)
        if not current_match:
            click.echo(
                "Error: could not parse current revision from alembic output. "
                "Use --onto to specify the target revision explicitly.",
                err=True,
            )
            sys.exit(1)
        onto = current_match.group(1)

    # Plan the rebase
    try:
        changes = plan_rebase(versions_dir, onto)
    except ValueError as e:
        click.echo(str(e))
        sys.exit(0)

    if not changes:
        click.echo("All branches already point to the target revision.")
        sys.exit(0)

    # Show planned changes
    click.echo(f"Rebasing onto {onto}:\n")
    for change in changes:
        filename = change.migration.path.name
        click.echo(f"  {filename}")
        click.echo(f"    down_revision: {change.old_down_revision} -> {change.new_down_revision}")
    click.echo()

    if dry_run:
        click.echo("Dry run — no changes made.")
        sys.exit(0)

    if not click.confirm("Apply these changes?"):
        click.echo("Aborted.")
        sys.exit(0)

    apply_rebase(changes)
    click.echo("Rebase complete.")


@main.command()
@click.option(
    "--user",
    "target",
    flag_value="user",
    default=True,
    help="Install to ~/.claude/skills/ (default)",
)
@click.option("--project", "target", flag_value="project", help="Install to ./.claude/skills/")
def skill(target: str) -> None:
    """Install the ch-migrate Claude skill.

    Copies the skill file to help Claude assist with ch-migrate integration.

    \b
    Locations:
      --user     ~/.claude/skills/ch-migrate/  (default, for all projects)
      --project  ./.claude/skills/ch-migrate/  (current project only)
    """
    # Find the skill bundled with this package
    skill_src = Path(__file__).parent / "skills" / "ch-migrate" / "SKILL.md"

    if not skill_src.exists():
        click.echo(f"Error: Skill file not found at {skill_src}", err=True)
        sys.exit(1)

    # Determine destination
    if target == "user":
        skill_dir = Path.home() / ".claude" / "skills" / "ch-migrate"
    else:
        skill_dir = Path.cwd() / ".claude" / "skills" / "ch-migrate"

    skill_dst = skill_dir / "SKILL.md"

    # Create directory and copy
    skill_dir.mkdir(parents=True, exist_ok=True)

    if skill_dst.exists():
        click.echo(f"Skill already exists at {skill_dst}")
        if not click.confirm("Overwrite?"):
            click.echo("Aborted.")
            return

    shutil.copy(skill_src, skill_dst)
    click.echo(f"Installed skill to {skill_dst}")


@main.command()
@click.argument("environment", required=False, default=None)
def lint(environment: str | None) -> None:
    """Lint upgrade statements with their source file and line.

    Without an environment, checks every local revision statically without
    credentials or a database connection.

    With an environment, checks only pending revisions and adds live size and
    dependency checks. Fails if the pending scope cannot be determined.

    \b
    Examples:
      ch-migrate lint              # Static only (CI-friendly)
      ch-migrate lint dev          # Static + runtime (needs DB)
    """
    from clickhouse_alembic.config import load_config
    from clickhouse_alembic.display import render_lint_report
    from clickhouse_alembic.lint import LintConfig, lint_migrations
    from clickhouse_alembic.rebase import build_revision_graph
    from clickhouse_alembic.statements import pending_revisions

    versions_dir = Path.cwd() / "migrations" / "versions"
    if not versions_dir.exists():
        click.echo("Error: migrations/versions/ not found", err=True)
        sys.exit(1)

    config_path = Path.cwd() / "config.yaml"
    lint_config = LintConfig()
    if config_path.exists():
        try:
            raw_config = load_config(config_path)
            lint_config = LintConfig.from_config(raw_config)
        except Exception:
            pass

    client = None
    database = None
    revisions = None

    if environment:
        try:
            env_config = get_env_config(environment, config_path)
            database = env_config["database"]

            from clickhouse_alembic.connection import get_client, get_current_heads

            revisions = pending_revisions(
                build_revision_graph(versions_dir), get_current_heads(env_config)
            )
            client = get_client(env_config)
        except Exception as e:
            raise click.ClickException(
                f"Could not resolve pending revisions for {environment}: {e}"
            ) from e

    try:
        report = lint_migrations(
            versions_dir,
            config=lint_config,
            client=client,
            database=database,
            revisions=revisions,
        )
    except (OSError, SyntaxError, ValueError) as e:
        raise click.ClickException(str(e)) from e
    finally:
        if client is not None:
            client.close()

    render_lint_report(report, runtime=environment is not None)

    sys.exit(1 if report.has_errors else 0)


@main.command()
@click.argument("environment")
@click.option("--validate", "-v", "validate_sql", type=click.Path(exists=True),
              help="Validate a SQL file against the dependency graph")
def deps(environment: str, validate_sql: str | None) -> None:
    """Show materialized view and dictionary dependency graph.

    Queries the live database to build a dependency graph of all tables,
    views, materialized views, and dictionaries, then renders it as a tree.

    Use --validate to check if a SQL file would break any dependencies.

    \b
    Examples:
      ch-migrate deps dev
      ch-migrate deps dev --validate migrations/sql/history/tables/users/drop.sql
    """
    from clickhouse_alembic.connection import get_client
    from clickhouse_alembic.deps import build_dependency_graph, validate_migration
    from clickhouse_alembic.display import render_dependency_tree

    config_path = Path.cwd() / "config.yaml"
    try:
        env_config = get_env_config(environment, config_path)
    except Exception as e:
        click.echo(f"Error loading config: {e}", err=True)
        sys.exit(1)

    database = env_config["database"]

    try:
        client = get_client(env_config)
    except Exception as e:
        click.echo(f"Error connecting to {environment}: {e}", err=True)
        sys.exit(1)

    click.echo(f"Building dependency graph for {environment} ({database})...")

    try:
        graph = build_dependency_graph(client, database)
    except Exception as e:
        click.echo(f"Error building dependency graph: {e}", err=True)
        sys.exit(1)

    render_dependency_tree(graph)

    if validate_sql:
        sql_content = Path(validate_sql).read_text()
        warnings = validate_migration(sql_content, graph)
        if warnings:
            click.echo()
            for w in warnings:
                style = "red" if w.severity == "error" else "yellow"
                click.echo(click.style(f"  [{w.severity.upper()}] {w.message}", fg=style))
            sys.exit(1)
        else:
            click.echo(click.style("\n  Migration validation passed.", fg="green"))


@main.command(name="diff")
@click.argument("environment")
@click.option(
    "--snapshot-dir",
    "-s",
    type=click.Path(exists=True),
    help="Path to a snapshot directory to compare against. Defaults to latest snapshot.",
)
def diff_cmd(environment: str, snapshot_dir: str | None) -> None:
    """Detect schema drift between local snapshot and live database.

    Compares the most recent snapshot (or a specified one) against the live
    database schema. Exit code 0 if in sync, 1 if drift detected.

    \b
    Examples:
      ch-migrate diff dev
      ch-migrate diff dev --snapshot-dir migrations/sql/snapshots/20260305_120000
    """
    from clickhouse_alembic.connection import get_client
    from clickhouse_alembic.diff import DiffStatus, compare_schemas
    from clickhouse_alembic.display import render_diff_report
    from clickhouse_alembic.introspect import Schema, get_live_schema, parse_create_statement

    config_path = Path.cwd() / "config.yaml"
    try:
        env_config = get_env_config(environment, config_path)
    except Exception as e:
        click.echo(f"Error loading config: {e}", err=True)
        sys.exit(1)

    database = env_config["database"]

    # Resolve snapshot directory
    if snapshot_dir:
        snap_path = Path(snapshot_dir)
    else:
        snapshots_base = Path.cwd() / "migrations" / "sql" / "snapshots"
        if not snapshots_base.exists():
            click.echo("Error: No snapshots found. Run 'ch-migrate snapshot' first.", err=True)
            sys.exit(1)
        dirs = sorted(snapshots_base.iterdir())
        if not dirs:
            click.echo("Error: No snapshots found. Run 'ch-migrate snapshot' first.", err=True)
            sys.exit(1)
        snap_path = dirs[-1]

    click.echo(f"Comparing snapshot {snap_path.name} against live {environment} ({database})...")

    # Load local schema from snapshot files
    local_schema = Schema(database=database)
    type_dirs = {
        "tables": "table",
        "views": "view",
        "materialized_views": "materialized_view",
        "dictionaries": "dictionary",
    }
    schema_attrs = {
        "table": local_schema.tables,
        "view": local_schema.views,
        "materialized_view": local_schema.materialized_views,
        "dictionary": local_schema.dictionaries,
    }

    for dir_name, obj_type in type_dirs.items():
        type_path = snap_path / dir_name
        if not type_path.exists():
            continue
        for sql_file in sorted(type_path.glob("*.sql")):
            ddl = sql_file.read_text()
            name = sql_file.stem
            parsed = parse_create_statement(ddl)
            if parsed:
                schema_attrs[obj_type][name] = parsed
            else:
                # Store minimal object with raw DDL
                from clickhouse_alembic.introspect import (
                    DictDefinition,
                    MVDefinition,
                    TableDefinition,
                    ViewDefinition,
                )
                fallback_types = {
                    "table": lambda: TableDefinition(name=name, engine="", raw_ddl=ddl),
                    "view": lambda: ViewDefinition(name=name, select_query="", raw_ddl=ddl),
                    "materialized_view": lambda: MVDefinition(name=name, raw_ddl=ddl),
                    "dictionary": lambda: DictDefinition(name=name, raw_ddl=ddl),
                }
                schema_attrs[obj_type][name] = fallback_types[obj_type]()

    # Get live schema
    try:
        client = get_client(env_config)
        live_schema = get_live_schema(client, database)
    except Exception as e:
        click.echo(f"Error connecting to {environment}: {e}", err=True)
        sys.exit(1)

    # Compare
    diffs = compare_schemas(local_schema, live_schema)
    render_diff_report(diffs)

    has_drift = any(d.status != DiffStatus.IN_SYNC for d in diffs)
    sys.exit(1 if has_drift else 0)


@main.command(name="upgrade-env")
def upgrade_env() -> None:
    """Regenerate migrations/env.py from the latest ch-migrate version.

    Updates the Alembic environment file to the latest version shipped with
    ch-migrate. This is needed when upgrading ch-migrate to pick up new
    features like execution hooks.

    The previous env.py is backed up as env.py.bak.
    """
    env_py_src = Path(__file__).parent / "env.py"
    env_py_dst = Path.cwd() / "migrations" / "env.py"

    if not env_py_dst.parent.exists():
        click.echo("Error: migrations/ directory not found. Run 'ch-migrate init' first.", err=True)
        sys.exit(1)

    if not env_py_src.exists():
        click.echo("Error: package env.py not found.", err=True)
        sys.exit(1)

    # Back up existing env.py if present
    if env_py_dst.exists():
        backup = env_py_dst.with_suffix(".py.bak")
        shutil.copy(env_py_dst, backup)
        click.echo(f"  Backed up existing env.py to {backup.name}")

    shutil.copy(env_py_src, env_py_dst)
    click.echo(f"  Updated migrations/env.py")
    click.echo("")
    click.echo("env.py has been upgraded. If you have custom modifications,")
    click.echo("compare with env.py.bak and reapply them.")


@main.command()
@click.argument("environment")
@click.option(
    "--exclude",
    "-e",
    multiple=True,
    help="Glob patterns to exclude (e.g., --exclude 'system_*' --exclude 'peerdb_*')",
)
@click.option(
    "--filter",
    "-f",
    "include_filter",
    multiple=True,
    help="Glob patterns to include (only matching objects are captured)",
)
def snapshot(environment: str, exclude: tuple[str, ...], include_filter: tuple[str, ...]) -> None:
    """Capture a schema snapshot from a live database.

    Connects to the environment and writes CREATE statements for all tables,
    views, materialized views, and dictionaries to a timestamped directory.

    \b
    Examples:
      ch-migrate snapshot dev
      ch-migrate snapshot dev --exclude 'system_*' --exclude 'peerdb_*'
      ch-migrate snapshot dev --filter 'geo_*'
    """
    import fnmatch

    from clickhouse_alembic.connection import get_client
    from clickhouse_alembic.display import render_snapshot_progress
    from clickhouse_alembic.introspect import Schema, get_live_schema

    config_path = Path.cwd() / "config.yaml"
    try:
        env_config = get_env_config(environment, config_path)
    except Exception as e:
        click.echo(f"Error loading config: {e}", err=True)
        sys.exit(1)

    database = env_config["database"]

    try:
        client = get_client(env_config)
    except Exception as e:
        click.echo(f"Error connecting to {environment}: {e}", err=True)
        sys.exit(1)

    click.echo(f"Capturing schema from {environment} ({database})...")

    try:
        schema = get_live_schema(client, database)
    except Exception as e:
        click.echo(f"Error introspecting database: {e}", err=True)
        sys.exit(1)

    # Build output directory
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    snapshot_dir = Path.cwd() / "migrations" / "sql" / "snapshots" / timestamp

    # Flatten all exclude patterns (support comma-separated within a single --exclude)
    exclude_patterns = []
    for pat in exclude:
        exclude_patterns.extend(p.strip() for p in pat.split(",") if p.strip())

    include_patterns = []
    for pat in include_filter:
        include_patterns.extend(p.strip() for p in pat.split(",") if p.strip())

    def should_include(name: str) -> bool:
        if include_patterns and not any(fnmatch.fnmatch(name, p) for p in include_patterns):
            return False
        if any(fnmatch.fnmatch(name, p) for p in exclude_patterns):
            return False
        return True

    # Write DDL files organized by type
    type_map = {
        "tables": schema.tables,
        "views": schema.views,
        "materialized_views": schema.materialized_views,
        "dictionaries": schema.dictionaries,
    }

    counts: dict[str, int] = {}
    excluded_count = 0

    for type_name, objects in type_map.items():
        count = 0
        for name, obj in objects.items():
            if not should_include(name):
                excluded_count += 1
                continue
            type_dir = snapshot_dir / type_name
            type_dir.mkdir(parents=True, exist_ok=True)
            ddl = obj.raw_ddl if obj.raw_ddl else f"-- No DDL captured for {name}\n"
            (type_dir / f"{name}.sql").write_text(ddl)
            count += 1
        counts[type_name] = count

    if sum(counts.values()) == 0:
        click.echo("No objects matched the filter criteria.", err=True)
        sys.exit(1)

    render_snapshot_progress(
        str(snapshot_dir.relative_to(Path.cwd())),
        counts,
        excluded=excluded_count,
    )


if __name__ == "__main__":
    main()
