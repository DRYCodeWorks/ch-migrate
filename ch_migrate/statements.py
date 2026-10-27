"""Extract migration SQL without importing or executing revision modules."""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ch_migrate.rebase import RevisionGraph
from ch_migrate.sql import split_statements

MigrationDirection = Literal["upgrade", "downgrade"]
_FSTRING_POSITIONS = sys.version_info >= (3, 12)


@dataclass(frozen=True)
class MigrationStatement:
    sql: str
    source: str
    line: int
    comments: tuple[str, ...]
    direction: MigrationDirection
    # False when the SQL only exists at run time; `sql` then holds the call's source.
    readable: bool = True


def migration_statements(path: Path) -> list[MigrationStatement]:
    """Collect the SQL in upgrade/downgrade bodies, preserving its source.

    Literal SQL is returned as statements. A call that sends SQL known only at run time
    (a variable, an expression) comes back as one unreadable statement, so callers can
    insist on a waiver instead of passing it silently.
    """
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
            elif _sends_sql(call):
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


def _sends_sql(call: ast.Call) -> bool:
    """`op.execute`, and `execute`/`exec_driver_sql` on a connection such as `op.get_bind()`."""
    return isinstance(call.func, ast.Attribute) and call.func.attr in ("execute", "exec_driver_sql")


def _file_statements(
    call: ast.Call, source: _Source, direction: MigrationDirection
) -> list[MigrationStatement]:
    path_value = (
        call.args[0].value if call.args and isinstance(call.args[0], ast.Constant) else None
    )
    if not isinstance(path_value, str):
        return [_unreadable(call, source, direction)]
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
        # `op.execute(read_sql("file.sql"))`: the file's statements are collected from read_sql.
        if isinstance(call.args[0], ast.Call) and _call_name(call.args[0]) in (
            "read_sql",
            "run_sql",
        ):
            return []
        return [_unreadable(call, source, direction)]
    comments = _python_comments(source.lines, call.lineno)
    filename = source.path.relative_to(source.root).as_posix()
    result = []
    for index, item in enumerate(split_statements(text)):
        # Inline expressions point to their AST call site, including escaped-newline strings.
        attached = comments + item.comments if index == 0 else item.comments
        result.append(MigrationStatement(item.sql, filename, call.lineno, attached, direction))
    return result


def _unreadable(
    call: ast.Call, source: _Source, direction: MigrationDirection
) -> MigrationStatement:
    code = ast.get_source_segment(source.content, call) or ast.unparse(call)
    filename = source.path.relative_to(source.root).as_posix()
    comments = _python_comments(source.lines, call.lineno)
    return MigrationStatement(code, filename, call.lineno, comments, direction, readable=False)


def _literal_sql(node: ast.AST, content: str) -> str | None:
    if _is_text_call(node):
        return _literal_sql(node.args[0], content)
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


def _is_text_call(node: ast.AST) -> bool:
    """SQLAlchemy's `text("...")` or `sa.text("...")` around the SQL."""
    if not isinstance(node, ast.Call) or len(node.args) != 1:
        return False
    func = node.func
    return (isinstance(func, ast.Name) and func.id == "text") or (
        isinstance(func, ast.Attribute) and func.attr == "text"
    )


def _python_comments(lines: list[str], lineno: int) -> tuple[str, ...]:
    comments = []
    index = lineno - 2
    while index >= 0 and lines[index].lstrip().startswith("#"):
        comments.append(lines[index].lstrip()[1:].strip())
        index -= 1
    return tuple(reversed(comments))
