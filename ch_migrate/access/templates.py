"""Named SQL statements from the package's `sql/` files.

Each statement in a file is named by the comment line directly above it. `{name}`
placeholders are filled in by `ch_migrate.sql.render_placeholders`; ClickHouse's own
`{name:Type}` query parameters are left for the server to bind.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Mapping

from ch_migrate.sql import render_placeholders, split_statements

SQL_DIR = Path(__file__).with_name("sql")


def statement(file: str, name: str, values: Mapping[str, str] | None = None) -> str:
    """The statement `name` from `sql/<file>.sql`, with its placeholders filled in."""
    return render_placeholders(_statements(file)[name], dict(values or {}))


@lru_cache(maxsize=None)
def _statements(file: str) -> dict[str, str]:
    text = (SQL_DIR / f"{file}.sql").read_text()
    return {sql.comments[-1]: sql.sql for sql in split_statements(text) if sql.comments}
