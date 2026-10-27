"""The little SQL the waiter needs: which statements can start work, and string literals."""

from __future__ import annotations

import re

from ch_migrate.sql import _skip_quoted_or_comment

# Statements that only read or change the session; everything else may start a mutation
# or distributed DDL. Waiting after a read costs one poll, so unknown forms count as writes.
_READS = frozenset(("SELECT", "SHOW", "DESCRIBE", "DESC", "EXPLAIN", "EXISTS", "SET", "USE"))
_WORD = re.compile(r"[A-Za-z]+")


def is_write(statement: str) -> bool:
    """True unless the statement's first keyword only reads or sets session state."""
    word = _WORD.match(_first_code(statement))
    return word is None or word.group(0).upper() not in _READS


def sql_string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _first_code(sql: str) -> str:
    """The statement from its first character that isn't whitespace or a comment."""
    i = 0
    while i < len(sql):
        if sql[i].isspace():
            i += 1
            continue
        skip_to = _skip_quoted_or_comment(sql, i)
        if skip_to is None or sql[i] in "'\"`":
            return sql[i:]
        i = skip_to
    return ""
