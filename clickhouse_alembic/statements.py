"""Extract migration SQL without importing or executing revision modules."""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from clickhouse_alembic.rebase import RevisionGraph
from clickhouse_alembic.sql import split_statements

MigrationDirection = Literal["upgrade", "downgrade"]
_FSTRING_POSITIONS = sys.version_info >= (3, 12)


@dataclass(frozen=True)
class MigrationStatement:
    sql: str
    source: str
    line: int
    comments: tuple[str, ...]
    direction: MigrationDirection


def migration_statements(path: Path) -> list[MigrationStatement]:
    """Collect literal SQL in upgrade/downgrade bodies, preserving its source."""
    content = path.read_text()
    root = path.parent.parent
    if root.name == "migrations":
        root = root.parent
    source = _Source(path, root, content, content.splitlines())
    result: list[MigrationStatement] = []
    for function in ast.parse(content).body:
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if function.name not in ("upgrade", "downgrade"):
            continue
        direction: MigrationDirection = "upgrade" if function.name == "upgrade" else "downgrade"
        calls = sorted(
            (node for node in ast.walk(function) if isinstance(node, ast.Call)),
            key=lambda node: (node.lineno, node.col_offset),
        )
        for call in calls:
            if _call_name(call) in ("read_sql", "run_sql"):
                result.extend(_file_statements(call, source, direction))
            elif _call_name(call) == "op.execute":
                result.extend(_inline_statements(call, source, direction))
    return result


def pending_revisions(graph: RevisionGraph, heads: set[str]) -> set[str]:
    """Subtract applied heads and their ancestors, as status resolves them."""
    applied: set[str] = set()
    unknown = heads - graph.migrations.keys()
    if unknown:
        raise ValueError("Database revisions are missing locally: " + ", ".join(sorted(unknown)))
    for head in heads:
        applied.update(graph.walk_to_root(head))
    return graph.migrations.keys() - applied


@dataclass(frozen=True)
class _Source:
    path: Path
    root: Path
    content: str
    lines: list[str]


def _call_name(call: ast.Call) -> str:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name):
        if call.func.attr in ("run_sql", "read_sql"):
            return call.func.attr
        return f"{call.func.value.id}.{call.func.attr}"
    return ""


def _file_statements(
    call: ast.Call, source: _Source, direction: MigrationDirection
) -> list[MigrationStatement]:
    if not call.args or not isinstance(call.args[0], ast.Constant):
        return []
    path_value = call.args[0].value
    if not isinstance(path_value, str):
        return []
    sql_path = source.path.parent.parent / "sql" / path_value
    statements = split_statements(sql_path.read_text())
    filename = sql_path.relative_to(source.root).as_posix()
    return [
        MigrationStatement(item.sql, filename, item.line, item.comments, direction)
        for item in statements
    ]


def _inline_statements(
    call: ast.Call, source: _Source, direction: MigrationDirection
) -> list[MigrationStatement]:
    if not call.args:
        return []
    text = _literal_sql(call.args[0], source.content)
    if text is None:
        return []
    comments = _python_comments(source.lines, call.lineno)
    filename = source.path.relative_to(source.root).as_posix()
    result = []
    for index, item in enumerate(split_statements(text)):
        # Inline expressions point to their AST call site, including escaped-newline strings.
        attached = comments + item.comments if index == 0 else item.comments
        result.append(MigrationStatement(item.sql, filename, call.lineno, attached, direction))
    return result


def _literal_sql(node: ast.AST, content: str) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if not isinstance(node, ast.JoinedStr):
        return None
    pieces = []
    for value in node.values:
        if isinstance(value, ast.Constant):
            pieces.append(value.value)
        elif isinstance(value, ast.FormattedValue):
            # Before 3.12, f-string expression nodes carry wrong source positions.
            segment = ast.get_source_segment(content, value.value) if _FSTRING_POSITIONS else None
            expression = segment or ast.unparse(value.value)
            conversion = "" if value.conversion == -1 else "!" + chr(value.conversion)
            format_spec = ""
            if value.format_spec is not None:
                format_spec = ":" + (_literal_sql(value.format_spec, content) or "")
            pieces.append("{" + expression + conversion + format_spec + "}")
    return "".join(pieces)


def _python_comments(lines: list[str], lineno: int) -> tuple[str, ...]:
    comments = []
    index = lineno - 2
    while index >= 0 and lines[index].lstrip().startswith("#"):
        comments.append(lines[index].lstrip()[1:].strip())
        index -= 1
    return tuple(reversed(comments))
