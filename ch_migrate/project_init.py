"""`ch-migrate init`: a new project's directories and starter files.

A new project gets an access.yaml whose migration role holds the `core` profile, with
commented examples (design note, Decision 5). Files that already exist are left alone.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import click

from ch_migrate import ui

# Each template under templates/project/, and the file it becomes in the project.
TEMPLATES = (
    ("alembic.ini", "alembic.ini"),
    ("config.yaml", "config.yaml"),
    ("access.yaml", "access.yaml"),
    ("env.local.example", ".env.local.example"),
    ("script.py.mako", "migrations/script.py.mako"),
)
DIRECTORIES = (
    "migrations/sql/bootstrap",
    "migrations/sql/history/tables",
    "migrations/sql/history/views",
    "migrations/sql/history/dictionaries",
    "migrations/versions",
)
NEXT_STEPS = (
    "  1. Point config.yaml at your ClickHouse servers.",
    "  2. Copy .env.local.example to .env.local and add the passwords.",
    "  3. Run `ch-migrate bootstrap dev`.",
    "  4. Run `ch-migrate new dev create_users --table users`, then write the SQL",
    "     in the .up.sql and .down.sql files it creates.",
)


@click.command()
@click.argument("path", default=".", type=click.Path())
@click.option("--name", "-n", default=None, help="Project name (defaults to directory name)")
def init(path: str, name: str | None) -> None:
    """Initialize a new ClickHouse migration project.

    Creates the project structure with config.yaml, access.yaml and the migrations directory.
    """
    project_path = Path(path).resolve()
    if name is None:
        name = project_path.name
    # Normalize project name (replace spaces/hyphens with underscores for database names)
    safe_name = name.replace("-", "_").replace(" ", "_").lower()
    ui.step(f"Creating ClickHouse migration project {name} in {project_path}")
    project_path.mkdir(parents=True, exist_ok=True)
    for directory in DIRECTORIES:
        (project_path / directory).mkdir(parents=True, exist_ok=True)
    _write_templates(project_path, safe_name)
    _write_env_py(project_path)
    _write_gitignore(project_path)
    ui.success("Project created. Next steps:")
    for line in NEXT_STEPS:
        ui.hint(line)


def get_template_path(name: str) -> Path:
    """Get path to a template file."""
    return Path(__file__).parent / "templates" / "project" / f"{name}.template"


def render_template(template_path: Path, **kwargs: str) -> str:
    """Render a template with substitutions."""
    content = template_path.read_text()
    for key, value in kwargs.items():
        content = content.replace(f"{{{key}}}", value)
    return content


def _write_templates(project_path: Path, safe_name: str) -> None:
    for template_name, output_name in TEMPLATES:
        output_path = project_path / output_name
        if output_path.exists():
            ui.detail(f"Skipped {output_name} (already exists)")
            continue
        content = render_template(get_template_path(template_name), project_name=safe_name)
        output_path.write_text(content)
        ui.detail(f"Created {output_name}")


def _write_env_py(project_path: Path) -> None:
    """Copy env.py from the package."""
    env_py_dst = project_path / "migrations" / "env.py"
    if not env_py_dst.exists():
        shutil.copy(Path(__file__).parent / "env.py", env_py_dst)
        ui.detail("Created migrations/env.py")


def _write_gitignore(project_path: Path) -> None:
    gitignore_path = project_path / ".gitignore"
    if not gitignore_path.exists():
        gitignore_path.write_text(".env.local\n__pycache__/\n*.pyc\n")
        ui.detail("Created .gitignore")
