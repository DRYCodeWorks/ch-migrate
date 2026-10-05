"""The files `ch-migrate new` writes for a SQL-first migration.

Alembic generates the revision file; this module rewrites it to run an upgrade
SQL file and a downgrade SQL file, and creates those files in the
object-centric history layout under migrations/sql/history/.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from ch_migrate.rebase import _MISSING, _literal_assignment

OBJECT_DIRS = {"table": "tables", "view": "views", "dictionary": "dictionaries"}
OTHER_DIR = "other"
REBUILD_REASON = (
    "Reverse a rebuild with another forward rebuild migration; automatic downgrade is unsupported."
)


@dataclass(frozen=True)
class NewOptions:
    """The authoring choices for one `new` invocation."""

    table_name: str | None = None
    view_name: str | None = None
    dict_name: str | None = None
    exchange: bool = False
    python_migration: bool = False
    irreversible_reason: str | None = None
    rebuild: bool = False

    @property
    def irreversible(self) -> str | None:
        return REBUILD_REASON if self.rebuild else self.irreversible_reason

    def named_objects(self) -> list[tuple[str, str]]:
        return [
            (kind, name)
            for kind, name in (
                ("table", self.table_name),
                ("view", self.view_name),
                ("dictionary", self.dict_name),
            )
            if name
        ]


@dataclass(frozen=True)
class RevisionHeader:
    """Identifiers Alembic wrote into a freshly generated revision file."""

    message: str
    revision: str
    down_revision: Any
    branch_labels: Any
    depends_on: Any
    docstring_source: str


@dataclass(frozen=True)
class SqlFiles:
    """Paths of a migration's SQL files, relative to migrations/sql/."""

    upgrade: str
    downgrade: str | None


def read_revision_header(path: Path) -> RevisionHeader:
    """Read the identifiers out of an Alembic-generated revision file."""
    content = path.read_text()
    module = ast.parse(content)
    docstring = ast.get_docstring(module) or ""

    def literal(name: str) -> Any:
        value = _literal_assignment(content, name)
        return None if value is _MISSING else value

    return RevisionHeader(
        message=docstring.splitlines()[0].strip() if docstring else path.stem,
        revision=str(literal("revision")),
        down_revision=literal("down_revision"),
        branch_labels=literal("branch_labels"),
        depends_on=literal("depends_on"),
        docstring_source=ast.get_source_segment(content, module.body[0]) if docstring else '""""""',
    )


def history_dir(object_type: str | None, object_name: str | None) -> PurePosixPath:
    """Where a migration's SQL files live, relative to migrations/sql/."""
    if object_type and object_name:
        return PurePosixPath("history", OBJECT_DIRS[object_type], object_name)
    return PurePosixPath("history", OTHER_DIR)


def write_sql_files(sql_root: Path, header: RevisionHeader, options: NewOptions) -> SqlFiles:
    """Create the upgrade (and, if reversible, downgrade) SQL files."""
    named = options.named_objects()
    rel_dir = history_dir(*named[0]) if named else history_dir(None, None)
    stem = f"{datetime.now().strftime('%Y_%m_%d_%H%M')}_{header.revision}_"
    stem += _slug(header.message)
    (sql_root / rel_dir).mkdir(parents=True, exist_ok=True)

    upgrade = rel_dir / f"{stem}.up.sql"
    (sql_root / upgrade).write_text(_sql_file_text(header, "upgrade"))
    if options.irreversible is not None:
        return SqlFiles(upgrade=str(upgrade), downgrade=None)

    downgrade = rel_dir / f"{stem}.down.sql"
    (sql_root / downgrade).write_text(_sql_file_text(header, "downgrade"))
    return SqlFiles(upgrade=str(upgrade), downgrade=str(downgrade))


def render_revision(header: RevisionHeader, files: SqlFiles, options: NewOptions) -> str:
    """The revision file for a SQL-first migration or guarded rebuild."""
    irreversible = options.irreversible
    operation = "rebuild_table" if options.rebuild else "run_sql"
    imports = f"IrreversibleMigration, {operation}" if irreversible else operation
    upgrade_call = (
        f"rebuild_table({options.table_name!r}, {files.upgrade!r})"
        if options.rebuild
        else f"run_sql({files.upgrade!r})"
    )
    marker = ""
    if irreversible:
        marker = (
            "\n# `ch-migrate down` refuses to revert past this migration.\n"
            f"irreversible = {irreversible!r}\n"
        )
        downgrade_body = "    raise IrreversibleMigration(revision, irreversible)"
    else:
        downgrade_body = f"    run_sql({files.downgrade!r})"

    return (
        f"{header.docstring_source}\n\n"
        f"from ch_migrate import {imports}\n\n"
        "# revision identifiers\n"
        f"revision = {header.revision!r}\n"
        f"down_revision = {header.down_revision!r}\n"
        f"branch_labels = {header.branch_labels!r}\n"
        f"depends_on = {header.depends_on!r}\n"
        f"{marker}\n\n"
        "def upgrade() -> None:\n"
        f"    {upgrade_call}\n\n\n"
        "def downgrade() -> None:\n"
        f"{downgrade_body}\n"
    )


def _slug(message: str) -> str:
    return re.sub(r"\W+", "_", message).strip("_").lower()[:40] or "migration"


def _sql_file_text(header: RevisionHeader, direction: str) -> str:
    guidance = {
        "upgrade": (
            "-- Write the statements for this change, each ending with a semicolon.\n"
            "-- They run one at a time, in order. Make each one safe to run twice\n"
            "-- (IF NOT EXISTS / IF EXISTS), so a failed run can simply be re-run.\n"
        ),
        "downgrade": (
            "-- Statements end with a semicolon and run one at a time, in order.\n"
            "-- If the change cannot be undone (dropped data does not come back),\n"
            "-- mark the migration irreversible instead.\n"
        ),
    }[direction]
    return (
        f"-- {header.message}: {direction}\n"
        f"-- Revision: {header.revision}\n"
        "--\n"
        f"{guidance}"
        "-- {db} is replaced with the environment's database name.\n\n"
    )
