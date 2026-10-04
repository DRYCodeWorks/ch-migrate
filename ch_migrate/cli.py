"""Command-line interface for ch-migrate-cli."""

from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click
from dotenv import load_dotenv

from ch_migrate import ui
from ch_migrate.authoring import NewOptions
from ch_migrate.config import get_env_config
from ch_migrate.json_output import (
    JsonCommand,
    command_failure,
    diff_document,
    emit_json,
    history_document,
    lint_document,
    status_document,
)
from ch_migrate.runner import alembic_failure, run_alembic, run_migrations

if TYPE_CHECKING:
    from ch_migrate.rebase import RevisionGraph

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


def _enforce_up_gate(environment: str, skip_mv_check: bool) -> None:
    from ch_migrate.gate import lint_pending_up

    try:
        report = lint_pending_up(Path.cwd(), environment, skip_mv_check)
    except Exception as error:
        raise click.ClickException(f"Migration preflight failed: {error}") from error
    if _print_gate_findings(report):
        raise click.ClickException(
            "Migration gate failed; nothing was applied. Fix SQL or add a reasoned in-file waiver."
        )


def _print_gate_findings(report) -> bool:
    from ch_migrate.lint import GATE_RULES, Severity

    blocked = False
    for finding in report.results:
        gate_error = finding.rule in GATE_RULES and finding.severity == Severity.ERROR
        blocked |= gate_error
        severity = finding.severity.value
        if severity == "error" and not gate_error:
            severity = "warn"
        position = finding.file or "config.yaml"
        if finding.line is not None:
            position += f":{finding.line}"
        click.echo(
            f"{position}: {severity.upper()} [{finding.rule}] {finding.message}", err=gate_error
        )
        if finding.statement:
            click.echo(f"  {finding.statement}", err=gate_error)
    return blocked


def _require_current_env() -> None:
    from ch_migrate.alembic_env import has_current_env

    if has_current_env(Path.cwd() / "migrations" / "env.py"):
        return
    raise click.ClickException(
        "This project's migrations/env.py is from ch-migrate 0.x. Run `ch-migrate upgrade-env`."
    )


def _refuse_irreversible_downgrade(environment: str, target: str) -> None:
    from ch_migrate.connection import get_migration_state
    from ch_migrate.downgrade import irreversible_reason, revisions_to_revert
    from ch_migrate.rebase import build_revision_graph

    try:
        env_config = get_env_config(environment, Path.cwd() / "config.yaml")
        heads = get_migration_state(env_config).heads
    except Exception:
        return  # `up`/`down` report configuration and connection errors themselves.
    graph = build_revision_graph(Path.cwd() / "migrations" / "versions")
    revisions = revisions_to_revert(graph, heads, target)
    if revisions is None:
        ui.warn("The downgrade range is unknown; relying on each migration's own refusal.")
        return
    reasons = {rev: irreversible_reason(graph, rev) for rev in revisions}
    if any(reason is not None for reason in reasons.values()):
        _report_irreversible_range(environment, graph, reasons)
        sys.exit(1)


def _report_irreversible_range(
    environment: str, graph: RevisionGraph, reasons: dict[str, str | None]
) -> None:
    """Show every migration the downgrade would revert, newest first, marking which can't be.

    Listing the whole range keeps it clear that only the marked migrations are
    irreversible, not everything the downgrade touches.
    """
    blocked = [rev for rev, reason in reasons.items() if reason is not None]
    if len(reasons) == 1:
        summary = "The migration it would revert is irreversible:"
    else:
        verb = "is" if len(blocked) == 1 else "are"
        summary = f"Of the {len(reasons)} migrations it would revert, {len(blocked)} {verb} irreversible:"
    ui.error(f"Downgrade refused; nothing was run. {summary}")
    for rev, reason in reasons.items():
        name = graph.migrations[rev].description or ""
        marker, status = ("✗", f"irreversible: {reason}") if reason is not None else (" ", "reversible")
        ui.detail(f"{marker} {rev[:8]}  {name}  ({status})", stderr=True)
    newest_blocked = blocked[0]
    above = list(reasons).index(newest_blocked)
    if above:
        ui.hint(
            f"Run `ch-migrate down {environment} -r {newest_blocked[:8]}` to revert only the "
            f"{_plural(above, 'migration')} above {newest_blocked[:8]}.",
            stderr=True,
        )
    if len(blocked) == 1:
        fix = f"To revert {newest_blocked[:8]}, write its downgrade and remove its irreversible marker"
    else:
        fix = "To revert them, write their downgrades and remove their irreversible markers"
    ui.hint(f"{fix} in a reviewed change.", stderr=True)


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


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

    ui.step(f"Creating ClickHouse migration project {name} in {project_path}")

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
            ui.detail(f"Skipped {output_name} (already exists)")
            continue

        content = render_template(template_path, project_name=safe_name)
        output_path.write_text(content)
        ui.detail(f"Created {output_name}")

    # Copy env.py from package
    env_py_src = Path(__file__).parent / "env.py"
    env_py_dst = project_path / "migrations" / "env.py"
    if not env_py_dst.exists():
        shutil.copy(env_py_src, env_py_dst)
        ui.detail("Created migrations/env.py")

    # Create .gitignore
    gitignore_path = project_path / ".gitignore"
    if not gitignore_path.exists():
        gitignore_path.write_text(".env.local\n__pycache__/\n*.pyc\n")
        ui.detail("Created .gitignore")

    ui.success("Project created. Next steps:")
    ui.hint("  1. Point config.yaml at your ClickHouse servers.")
    ui.hint("  2. Copy .env.local.example to .env.local and add the passwords.")
    ui.hint("  3. Run `ch-migrate bootstrap dev`.")
    ui.hint("  4. Run `ch-migrate new dev create_users --table users`, then write the SQL")
    ui.hint("     in the .up.sql and .down.sql files it creates.")


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
    from ch_migrate.bootstrap import run_bootstrap

    try:
        run_bootstrap(environment, dry_run=dry_run, verbose=verbose)
    except Exception as e:
        ui.fail(f"Bootstrap failed: {e}")


@main.command()
@click.argument("environment")
@click.option("--revision", "-r", default="head", help="Revision to upgrade to (default: head)")
@click.option(
    "--timeout",
    type=click.FloatRange(min=0, min_open=True),
    default=None,
    help="Maximum total waiting seconds; default waits without a deadline",
)
@click.option(
    "--skip-mv-check",
    is_flag=True,
    help="Skip materialized view declaration validation",
)
@click.option("--verbose", is_flag=True, help="Show the full traceback if a migration fails")
def up(environment: str, revision: str, skip_mv_check: bool, timeout: float | None, verbose: bool) -> None:
    """Apply pending migrations.

    Runs all unapplied migrations to bring the database to the latest version.
    Use --revision to upgrade to a specific revision instead of head.

    Idempotency and standalone-SET gate errors refuse the run before Alembic.
    Other findings are warnings; --skip-mv-check only skips MV declaration checks.
    """
    if timeout is not None and not math.isfinite(timeout):
        raise click.BadParameter("must be finite", param_hint="--timeout")
    _require_current_env()
    _enforce_up_gate(environment, skip_mv_check)
    from ch_migrate.migration_runner import run_upgrade

    try:
        run_upgrade(environment, revision, timeout)
    except Exception as error:
        if verbose:
            raise
        raise click.ClickException(str(error)) from error


@main.command()
@click.argument("environment")
@click.option("--revision", "-r", default="-1", help="Revision to downgrade to (default: -1)")
@click.option("--verbose", is_flag=True, help="Show the full traceback if a migration fails")
def down(environment: str, revision: str, verbose: bool) -> None:
    """Rollback migrations.

    By default, rolls back the last migration. Use --revision to specify a target.
    """
    _require_current_env()
    _refuse_irreversible_downgrade(environment, revision)
    sys.exit(run_migrations(environment, ["downgrade", revision], verbose=verbose))


@main.command(cls=JsonCommand)
@click.argument("environment")
@click.option("--json", "json_output", is_flag=True, help="Emit a versioned JSON document")
def status(environment: str, json_output: bool) -> None:
    """Show migration status.

    Displays environment info, applied/pending counts, and head status.
    Status is a report: it exits 0 even when the database cannot be reached or
    migrations are pending, and exits 1 only when the config or migrations/versions/
    is missing. CI jobs use it as a non-blocking reporter.
    """
    from ch_migrate.display import render_status

    state = _load_migration_state(environment)
    if json_output:
        if state.db_error is not None:
            command_failure(state.db_error)
        document = status_document(state.graph, state.heads, state.env_config["database"])
        emit_json("status", document)
        sys.exit(0 if document["at_head"] else 1)
    render_status(environment, state.env_config, state.graph, state.applied, db_error=state.db_error)
    if state.db_error:
        ui.warn(f"Could not reach the database: {state.db_error.strip().splitlines()[0]}")
        return
    pending = len(set(state.graph.migrations) - (state.applied or set()))
    if pending:
        noun = "migration" if pending == 1 else "migrations"
        ui.hint(f"Run `ch-migrate up {environment}` to apply {pending} pending {noun}.")


@main.command(cls=JsonCommand)
@click.argument("environment")
@click.option("--json", "json_output", is_flag=True, help="Emit a versioned JSON document")
def history(environment: str, json_output: bool) -> None:
    """Show migration history.

    Displays a tree of all migrations, color-coded by applied status.
    """
    _require_current_env()
    from ch_migrate.display import render_history

    state = _load_migration_state(environment)
    if json_output:
        document = history_document(
            state.graph, None if state.heads - set(state.graph.migrations) else state.applied
        )
        if state.db_error is not None:
            document["error"] = state.db_error
        emit_json("history", document)
        sys.exit(2 if state.db_error is not None else 0)
    render_history(state.graph, state.applied, db_error=state.db_error)


@dataclass(frozen=True)
class _MigrationState:
    env_config: dict[str, Any]
    graph: RevisionGraph
    applied: set[str] | None  # None when the database could not be read
    db_error: str | None
    heads: set[str]


def _load_migration_state(environment: str) -> _MigrationState:
    """Local revision graph plus what the database says is applied."""
    from ch_migrate.connection import get_migration_state
    from ch_migrate.rebase import build_revision_graph

    env_config = _env_config_or_fail(environment)
    graph = build_revision_graph(_versions_dir_or_fail())
    try:
        state = get_migration_state(env_config)
        heads = state.heads
        if warning := state.version_table.warning():
            ui.warn(warning)
    except Exception as e:
        return _MigrationState(env_config, graph, None, str(e), set())
    applied: set[str] = set()
    unknown = [head for head in heads if head not in graph.migrations]
    for head in heads:
        if head in graph.migrations:
            applied.update(graph.walk_to_root(head))
    for head in unknown:
        ui.warn(f"The database is at {head[:12]}, which is not in your local migration files.")
    if unknown:
        ui.warn("Applied status may be incomplete; pull the missing revisions.")
    return _MigrationState(env_config, graph, applied, None, heads)


def _env_config_or_fail(environment: str) -> dict[str, Any]:
    try:
        return get_env_config(environment, Path.cwd() / "config.yaml")
    except Exception as e:
        if click.get_current_context().params.get("json_output"):
            command_failure(f"Error loading config: {e}")
        ui.fail(f"Could not load config: {e}")


def _versions_dir_or_fail() -> Path:
    versions_dir = Path.cwd() / "migrations" / "versions"
    if not versions_dir.exists():
        if click.get_current_context().params.get("json_output"):
            command_failure("Error: migrations/versions/ not found")
        ui.fail("migrations/versions/ not found.", "Run `ch-migrate init` to create a project.")
    return versions_dir


def _check_mv_declarations() -> None:
    """Refuse `up` when a materialized-view migration lacks its declarations."""
    from ch_migrate.config import load_config
    from ch_migrate.lint import LintConfig
    from ch_migrate.mv_validate import validate_mv_migrations

    versions_dir = Path.cwd() / "migrations" / "versions"
    if not versions_dir.exists():
        return
    cutoff = None
    config_path = Path.cwd() / "config.yaml"
    if config_path.exists():
        try:
            cutoff = LintConfig.from_config(load_config(config_path)).mv_validation_cutoff
        except Exception as e:
            ui.warn(f"Could not load lint config: {e}")
    mv_errors = validate_mv_migrations(versions_dir, cutoff_date=cutoff)
    if not mv_errors:
        return
    ui.error("Materialized view declarations are incomplete; nothing was run.")
    for error in mv_errors:
        where = f"{error.file} ({error.mv_name})" if error.mv_name else error.file
        ui.detail(f"{where}: {error.message}", stderr=True)
    ui.hint("Fix these, or pass `--skip-mv-check` to run anyway.", stderr=True)
    sys.exit(1)


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
    result = run_alembic(environment, ["revision", "-m", name])
    if result.returncode != 0:
        ui.fail(f"Could not create the revision: {alembic_failure(result)}")
    migration_path = _find_migration_file(result.stdout)
    if migration_path is None:
        ui.fail("Could not find the revision file Alembic generated.")
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
    for problem in problems:
        ui.error(problem)
    if problems:
        sys.exit(1)


def _create_sql_first_migration(migration_path: Path, options: NewOptions) -> None:
    from ch_migrate.authoring import read_revision_header, render_revision, write_sql_files

    header = read_revision_header(migration_path)
    files = write_sql_files(Path.cwd() / "migrations" / "sql", header, options)
    migration_path.write_text(render_revision(header, files, options.irreversible_reason))
    ui.success(f"Created migration {header.revision[:8]}  {header.message}")
    ui.detail(f"migrations/sql/{files.upgrade}")
    if files.downgrade:
        ui.detail(f"migrations/sql/{files.downgrade}")
        ui.hint("Write the SQL in these files; the revision needs no edits.")
    else:
        ui.hint("Write the SQL in this file; the revision needs no edits.")
        ui.hint("It is marked irreversible, so `ch-migrate down` will refuse to revert it.")


def _create_python_migration(migration_path: Path, options: NewOptions) -> None:
    from ch_migrate.authoring import read_revision_header

    header = read_revision_header(migration_path)
    ui.success(f"Created migration {header.revision[:8]}  {header.message}")
    ui.detail(str(migration_path.relative_to(Path.cwd())))
    named = options.named_objects()
    if named:
        object_type, object_name = named[0]
        sql_path = _create_sql_file(object_name, object_type, header.revision)
        if sql_path:
            ui.detail(str(sql_path.relative_to(Path.cwd())))
    ui.hint("Write upgrade() and downgrade() in the revision file.")


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
    from ch_migrate.scaffold import (
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
            ui.detail(f"Fetched the current DDL for {table_name}")
        dict_names = find_dependent_dictionaries(env_config, table_name)
        if dict_names:
            ui.detail(f"Found dependent dictionaries: {', '.join(dict_names)}")
    except Exception:
        ui.warn("Could not connect to the database; the scaffold uses placeholder DDL.")

    # Create SQL history file with shadow table DDL
    sql_content = generate_exchange_sql(table_name, current_ddl)
    sql_path = _create_sql_file(table_name, "table", revision)
    if sql_path:
        sql_path.write_text(sql_content)

        # Rewrite the migration .py with EXCHANGE pattern
        migration_path = _find_migration_file(alembic_stdout)
        if migration_path:
            rel_sql = str(sql_path.relative_to(Path.cwd() / "migrations" / "sql"))
            rewrite_migration_file(migration_path, table_name, rel_sql, dict_names or None)
            ui.success(f"Created EXCHANGE TABLES migration {revision[:8]} for {table_name}")
            ui.detail(str(migration_path.relative_to(Path.cwd())))
            ui.detail(str(sql_path.relative_to(Path.cwd())))
            ui.hint("Edit the shadow table's CREATE statement in the SQL file.")
        else:
            ui.warn("Could not locate the migration file to rewrite.")


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
    from ch_migrate.rebase import apply_rebase, plan_rebase

    versions_dir = _versions_dir_or_fail()

    # Check for uncommitted changes to migration files
    result = subprocess.run(
        ["git", "diff", "--name-only", "migrations/versions/"],
        capture_output=True,
        text=True,
        cwd=Path.cwd(),
    )
    if result.returncode == 0 and result.stdout.strip():
        ui.fail("Migration files have uncommitted changes.", "Commit or stash them first.")

    if onto is None:
        onto = _deployed_head(environment)

    try:
        changes = plan_rebase(versions_dir, onto)
    except ValueError as e:
        ui.hint(str(e))
        sys.exit(0)

    if not changes:
        ui.success("All branches already point to the target revision.")
        sys.exit(0)

    ui.step(f"Rebasing onto {onto}:")
    for change in changes:
        ui.detail(change.migration.path.name)
        ui.detail(f"  down_revision: {change.old_down_revision} -> {change.new_down_revision}")

    if dry_run:
        ui.hint("Dry run; no changes made.")
        sys.exit(0)

    if not click.confirm("Apply these changes?"):
        ui.hint("Aborted.")
        sys.exit(0)

    apply_rebase(changes)
    ui.success("Rebase complete.")


def _deployed_head(environment: str) -> str:
    """The revision `alembic current` reports for this environment."""
    explicit = "Use `--onto REVISION` to name the target explicitly."
    result = run_alembic(environment, ["current"])
    if result.returncode != 0:
        ui.fail(f"Could not read the current revision: {alembic_failure(result)}", explicit)
    # Format: "abc123 (head)" or "abc123"
    current = re.search(r"(\w{4,})", result.stdout)
    if current is None:
        ui.fail("Could not parse the current revision from Alembic's output.", explicit)
    return current.group(1)


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
        ui.fail(f"Skill file not found at {skill_src}")

    # Determine destination
    if target == "user":
        skill_dir = Path.home() / ".claude" / "skills" / "ch-migrate"
    else:
        skill_dir = Path.cwd() / ".claude" / "skills" / "ch-migrate"

    skill_dst = skill_dir / "SKILL.md"

    # Create directory and copy
    skill_dir.mkdir(parents=True, exist_ok=True)

    if skill_dst.exists():
        ui.warn(f"A skill already exists at {skill_dst}")
        if not click.confirm("Overwrite?"):
            ui.hint("Aborted.")
            return

    shutil.copy(skill_src, skill_dst)
    ui.success(f"Installed the skill to {skill_dst}")


@main.command(cls=JsonCommand)
@click.argument("environment")
@click.option("--json", "json_output", is_flag=True, help="Emit a versioned JSON document")
def plan(environment: str, json_output: bool) -> None:
    """Inspect pending upgrades, rewrite bytes, rebuild risks and lint without executing."""
    from ch_migrate.plan import build_plan, render_plan

    try:
        document = build_plan(Path.cwd(), environment)
    except Exception as error:
        if json_output:
            emit_json("plan", {"error": str(error)})
        else:
            click.echo(f"Error: {error}", err=True)
        raise click.exceptions.Exit(2) from error
    if json_output:
        emit_json("plan", document)
    else:
        render_plan(document)
    raise click.exceptions.Exit(1 if document["gate_would_refuse"] else 0)


@main.command(cls=JsonCommand)
@click.argument("environment", required=False, default=None)
@click.option("--json", "json_output", is_flag=True, help="Emit a versioned JSON document")
def lint(environment: str | None, json_output: bool) -> None:
    """Lint upgrade statements with their source file and line.

    Without an environment, checks revisions after the gate baseline statically,
    without credentials or a database connection.

    With an environment, checks only pending revisions and adds live dependency
    checks. Use plan for rewrite sizes. Fails if the pending scope cannot be determined.

    \b
    Examples:
      ch-migrate lint              # Static only (CI-friendly)
      ch-migrate lint dev          # Static + runtime (needs DB)
    """
    from ch_migrate.config import load_config
    from ch_migrate.display import render_lint_report
    from ch_migrate.lint import LintConfig, lint_migrations
    from ch_migrate.rebase import build_revision_graph
    from ch_migrate.statements import pending_revisions

    versions_dir = _versions_dir_or_fail()

    config_path = Path.cwd() / "config.yaml"
    lint_config = LintConfig()
    if config_path.exists():
        try:
            raw_config = load_config(config_path)
            lint_config = LintConfig.from_config(raw_config)
        except Exception as error:
            raise click.ClickException(f"Invalid lint configuration: {error}") from error

    client = None
    database = None
    revisions = None

    if environment:
        try:
            env_config = get_env_config(environment, config_path)
            database = env_config["database"]

            from ch_migrate.connection import get_client, get_migration_state

            revisions = pending_revisions(
                build_revision_graph(versions_dir), get_migration_state(env_config).heads
            )
            client = get_client(env_config)
        except Exception as e:
            if json_output:
                command_failure(f"Could not work out the pending revisions for {environment}: {e}")
            ui.fail(f"Could not work out the pending revisions for {environment}: {e}")

    try:
        report = lint_migrations(
            versions_dir,
            config=lint_config,
            client=client,
            database=database,
            revisions=revisions,
        )
    except (OSError, SyntaxError, ValueError) as e:
        if json_output:
            command_failure(str(e))
        ui.fail(str(e))
    finally:
        if client is not None:
            client.close()

    if json_output:
        emit_json("lint", lint_document(report))
    else:
        render_lint_report(report, runtime=environment is not None)

    sys.exit(1 if report.has_errors else 0)


@main.command()
@click.argument("environment")
@click.option(
    "--validate",
    "-v",
    "validate_sql",
    type=click.Path(exists=True),
    help="Validate a SQL file against the dependency graph",
)
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
    from ch_migrate.connection import get_client
    from ch_migrate.deps import build_dependency_graph, validate_migration
    from ch_migrate.display import render_dependency_tree

    env_config = _env_config_or_fail(environment)
    database = env_config["database"]

    try:
        client = get_client(env_config)
    except Exception as e:
        ui.fail(f"Could not connect to {environment}: {e}")

    ui.step(f"Reading dependencies in {environment} ({database})")

    try:
        graph = build_dependency_graph(client, database)
    except Exception as e:
        ui.fail(f"Could not build the dependency graph: {e}")

    render_dependency_tree(graph)

    if validate_sql:
        sql_content = Path(validate_sql).read_text()
        warnings = validate_migration(sql_content, graph)
        for w in warnings:
            (ui.error if w.severity == "error" else ui.warn)(w.message)
        if warnings:
            sys.exit(1)
        ui.success(f"Validation passed: {validate_sql} keeps every dependency intact.")


@main.command(name="diff", cls=JsonCommand)
@click.argument("environment")
@click.option("--json", "json_output", is_flag=True, help="Emit a versioned JSON document")
@click.option(
    "--snapshot-dir",
    "-s",
    type=click.Path(exists=True),
    help="Path to a snapshot directory to compare against. Defaults to latest snapshot.",
)
def diff_cmd(environment: str, snapshot_dir: str | None, json_output: bool) -> None:
    """Detect schema drift between local snapshot and live database.

    Compares the most recent snapshot (or a specified one) against the live
    database schema. Exit code 0 if in sync, 1 if drift detected.

    \b
    Examples:
      ch-migrate diff dev
      ch-migrate diff dev --snapshot-dir migrations/sql/snapshots/20260305_120000
    """
    from ch_migrate.connection import get_client
    from ch_migrate.diff import DiffStatus, compare_schemas
    from ch_migrate.display import render_diff_report
    from ch_migrate.introspect import (
        VERSION_TABLE,
        Schema,
        get_live_schema,
        parse_create_statement,
    )

    env_config = _env_config_or_fail(environment)
    database = env_config["database"]

    # Resolve snapshot directory
    if snapshot_dir:
        snap_path = Path(snapshot_dir)
    else:
        snapshots_base = Path.cwd() / "migrations" / "sql" / "snapshots"
        dirs = sorted(snapshots_base.iterdir()) if snapshots_base.exists() else []
        if not dirs:
            if json_output:
                command_failure(f"No snapshots found. Run `ch-migrate snapshot {environment}` first.")
            ui.fail("No snapshots found.", f"Run `ch-migrate snapshot {environment}` first.")
        snap_path = dirs[-1]

    if not json_output:
        ui.step(f"Comparing snapshot {snap_path.name} with {environment} ({database})")

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
            name = sql_file.stem
            if name == VERSION_TABLE:
                continue  # older snapshots captured Alembic's own table
            ddl = sql_file.read_text()
            parsed = parse_create_statement(ddl)
            if parsed:
                schema_attrs[obj_type][name] = parsed
            else:
                # Store minimal object with raw DDL
                from ch_migrate.introspect import (
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
        try:
            live_schema = get_live_schema(client, database)
        finally:
            client.close()
    except Exception as e:
        if json_output:
            command_failure(f"Error connecting to {environment}: {e}")
        ui.fail(f"Could not read the live schema from {environment}: {e}")

    # Compare
    diffs = compare_schemas(local_schema, live_schema)
    if json_output:
        emit_json("diff", diff_document(diffs))
    else:
        render_diff_report(diffs)

    if any(d.status != DiffStatus.IN_SYNC for d in diffs):
        ui.hint(
            f"Write a migration for it, or run `ch-migrate snapshot {environment}` "
            "to accept the live schema."
        )
        sys.exit(1)


@main.command(name="upgrade-env")
def upgrade_env() -> None:
    """Regenerate migrations/env.py from the latest ch-migrate version.

    Updates the Alembic environment file to the latest version shipped with
    ch-migrate. This is needed when upgrading ch-migrate to pick up new
    features. Records current script heads as the lint gate baseline.

    The previous env.py is backed up as env.py.bak.
    """
    env_py_src = Path(__file__).parent / "env.py"
    env_py_dst = Path.cwd() / "migrations" / "env.py"

    if not env_py_dst.parent.exists():
        ui.fail("migrations/ not found.", "Run `ch-migrate init` first.")

    if not env_py_src.exists():
        ui.fail("The package's env.py is missing; reinstall ch-migrate-cli.")

    from ch_migrate.baseline import record_baseline

    try:
        heads = record_baseline(Path.cwd())
    except Exception as error:
        raise click.ClickException(f"Could not record gate baseline: {error}") from error
    click.echo("  Recorded gate baseline: " + (", ".join(heads) if heads else "(empty)"))
    # This command stays offline; status performs the live check for a selected environment.
    click.echo(
        "Warning: existing version tables are not converted. If the deployment is replicated, "
        "run `ch-migrate status <env>` and follow README 'The version table' to back up, "
        "reconcile, and manually convert non-replicated state before routing across nodes.",
        err=True,
    )

    if env_py_dst.exists() and env_py_dst.read_bytes() == env_py_src.read_bytes():
        click.echo("migrations/env.py is already current; backup unchanged.")
        return

    # Back up existing env.py if present
    if env_py_dst.exists():
        backup = env_py_dst.with_suffix(".py.bak")
        shutil.copy(env_py_dst, backup)
        ui.detail(f"Backed up the existing env.py to migrations/{backup.name}")

    shutil.copy(env_py_src, env_py_dst)
    ui.success("Updated migrations/env.py")
    ui.hint("If you had custom changes, compare with env.py.bak and reapply them.")


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

    from ch_migrate.connection import get_client
    from ch_migrate.display import render_snapshot_progress
    from ch_migrate.introspect import Schema, get_live_schema

    env_config = _env_config_or_fail(environment)
    database = env_config["database"]

    try:
        client = get_client(env_config)
    except Exception as e:
        ui.fail(f"Could not connect to {environment}: {e}")

    ui.step(f"Capturing the schema of {environment} ({database})")

    try:
        schema = get_live_schema(client, database)
    except Exception as e:
        ui.fail(f"Could not read the schema: {e}")

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
        ui.fail("No objects matched the filters.")

    render_snapshot_progress(
        str(snapshot_dir.relative_to(Path.cwd())),
        counts,
        excluded=excluded_count,
    )


if __name__ == "__main__":
    main()
