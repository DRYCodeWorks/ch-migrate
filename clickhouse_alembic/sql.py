"""SQL files as migrations: placeholder rendering, statement splitting, execution.

The ClickHouse HTTP interface accepts one statement per request, so a SQL file
holding several statements is split here and run one statement at a time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import chain
from pathlib import Path
from typing import Any, Iterator

_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_HEREDOC_OPEN = re.compile(r"\$(\w*)\$")


@dataclass(frozen=True)
class Statement:
    """One statement from a SQL file.

    Attributes:
        sql: The statement text, without its trailing semicolon or the comments
            above it.
        line: 1-based line in the file where the statement text begins.
        comments: Comment lines directly above the statement, without their
            comment markers. Waivers are written here.
    """

    sql: str
    line: int
    comments: tuple[str, ...] = ()


def run_sql(path: str, **values: Any) -> None:
    """Run every statement in a SQL file under migrations/sql/, one at a time.

    `{db}`, `{cluster}` and `{on_cluster}` are filled in from the environment;
    keyword arguments add to or override them. Other braces are left alone.

    Example:
        run_sql("history/tables/logs/2026_10_02_1430_a1b2c3_add_status.up.sql")
    """
    from alembic import context, op

    statements = load_statements(path, **values)
    if not statements:
        # An empty file is almost always a migration someone forgot to fill in.
        # Applying it would record the revision as done with nothing changed.
        raise ValueError(f"SQL file has no statements: migrations/sql/{path}")
    if context.is_offline_mode():
        for statement in statements:
            op.execute(statement.sql.replace(":", r"\:"))
    else:
        connection = op.get_bind()
        for statement in statements:
            connection.exec_driver_sql(statement.sql.replace("%", "%%"))


def load_statements(path: str, **values: Any) -> list[Statement]:
    """Read, render and split a SQL file under migrations/sql/."""
    sql_path = Path.cwd() / "migrations" / "sql" / path
    if not sql_path.exists():
        raise FileNotFoundError(f"SQL file not found: {sql_path}")
    return split_statements(render_placeholders(sql_path.read_text(), default_values(values)))


def default_values(overrides: dict[str, Any]) -> dict[str, Any]:
    """Placeholder values from the environment, with explicit overrides applied."""
    from clickhouse_alembic.helpers import get_cluster, get_db, on_cluster

    values: dict[str, Any] = {
        "db": get_db(),
        "cluster": get_cluster() or "",
        "on_cluster": on_cluster(),
    }
    values.update(overrides)
    return values


def render_placeholders(sql: str, values: dict[str, Any]) -> str:
    """Replace `{name}` for each known name; leave every other brace as written."""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        return str(values[name]) if name in values else match.group(0)

    return _PLACEHOLDER.sub(replace, sql)


def split_statements(sql: str) -> list[Statement]:
    """Split SQL text into statements on semicolons outside quotes and comments."""
    statements: list[Statement] = []
    start, line = 0, 1
    for end in chain(_statement_ends(sql), (len(sql),)):
        chunk = sql[start:end]
        offset, comments = _leading_comments(chunk)
        text = chunk[offset:].strip()
        if text:
            statements.append(Statement(text, line + chunk.count("\n", 0, offset), tuple(comments)))
        line += sql.count("\n", start, end + 1)
        start = end + 1
    return statements


def _statement_ends(sql: str) -> Iterator[int]:
    """Offsets of the semicolons that end statements."""
    i = 0
    while i < len(sql):
        skip_to = _skip_quoted_or_comment(sql, i)
        if skip_to is not None:
            i = skip_to
            continue
        if sql[i] == ";":
            yield i
        i += 1


def _skip_quoted_or_comment(sql: str, i: int) -> int | None:
    """If a string, quoted identifier, comment or heredoc starts at i, return its end."""
    char = sql[i]
    if char in "'\"`":
        return _end_of_quoted(sql, i, char)
    if sql.startswith("--", i) or sql.startswith("#!", i) or sql.startswith("# ", i):
        newline = sql.find("\n", i)
        return len(sql) if newline == -1 else newline + 1
    if sql.startswith("/*", i):
        close = sql.find("*/", i + 2)
        return len(sql) if close == -1 else close + 2
    if char == "$":
        match = _HEREDOC_OPEN.match(sql, i)
        if match:
            close = sql.find(match.group(0), match.end())
            return len(sql) if close == -1 else close + len(match.group(0))
    return None


def _end_of_quoted(sql: str, i: int, quote: str) -> int:
    """End offset (exclusive) of a quoted run, honouring backslash and doubled quotes."""
    j = i + 1
    while j < len(sql):
        if sql[j] == "\\":
            j += 2
            continue
        if sql[j] == quote:
            if sql.startswith(quote * 2, j):
                j += 2
                continue
            return j + 1
        j += 1
    return len(sql)


def _leading_comments(chunk: str) -> tuple[int, list[str]]:
    """Skip whitespace and comments at the start of chunk.

    Returns the offset of the first SQL character and the comment texts seen,
    keeping only the comments in the contiguous block directly above the SQL.
    """
    comments: list[str] = []
    i = 0
    at_line_start = True  # a line comment consumes its own newline
    while i < len(chunk):
        run_end = i
        while run_end < len(chunk) and chunk[run_end].isspace():
            run_end += 1
        newlines = chunk.count("\n", i, run_end)
        if newlines >= (1 if at_line_start else 2):
            comments = []  # a blank line separates a comment block from the SQL
        i = run_end
        end = _skip_quoted_or_comment(chunk, i) if i < len(chunk) else None
        if end is None or chunk[i] in "'\"`$":
            break
        comments.extend(_comment_text(chunk[i:end]).splitlines())
        at_line_start = chunk[end - 1] == "\n"
        i = end
    return i, comments


def _comment_text(comment: str) -> str:
    """Strip comment markers from one comment."""
    text = comment.strip()
    for marker in ("--", "#!", "#"):
        if text.startswith(marker):
            return text[len(marker) :].strip()
    if text.startswith("/*"):
        return text[2:-2].strip() if text.endswith("*/") else text[2:].strip()
    return text
