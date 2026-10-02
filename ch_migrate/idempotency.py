"""Statement-level idempotency classification, without evaluating SQL literals."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from ch_migrate.sql import _skip_quoted_or_comment

_WORD = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")
_OBJECTS = (
    ("MATERIALIZED", "VIEW"),
    ("ROW", "POLICY"),
    ("SETTINGS", "PROFILE"),
    ("NAMED", "COLLECTION"),
    ("TABLE",),
    ("VIEW",),
    ("DICTIONARY",),
    ("DATABASE",),
    ("USER",),
    ("ROLE",),
    ("QUOTA",),
    ("FUNCTION",),
)
_ACTIONS = {
    "ADD",
    "DROP",
    "RENAME",
    "MODIFY",
    "MATERIALIZE",
    "CLEAR",
    "UPDATE",
    "DELETE",
    "ATTACH",
    "DETACH",
    "MOVE",
    "REPLACE",
    "COMMENT",
    "RESET",
    "FREEZE",
    "UNFREEZE",
}
_WAIVER = re.compile(r"^ch-migrate:\s*allow-non-idempotent(?:\s+(.*))?$", re.IGNORECASE)


@dataclass(frozen=True)
class IdempotencyCheck:
    status: Literal["ok", "fix", "waiver"]
    suggestion: str = ""


def classify_idempotency(sql: str) -> IdempotencyCheck:
    tokens = _tokens(sql)
    if not tokens:
        return IdempotencyCheck("ok")
    first = tokens[0]
    if first in ("INSERT", "UPDATE") or tokens[:2] == ("DELETE", "FROM"):
        return IdempotencyCheck("waiver")
    if tokens[:2] == ("EXCHANGE", "TABLES"):
        return IdempotencyCheck("waiver")
    if first == "RENAME" and len(tokens) > 1 and tokens[1] in ("TABLE", "DICTIONARY", "DATABASE"):
        return IdempotencyCheck("waiver")
    if first in ("CREATE", "DROP", "ATTACH", "DETACH"):
        return _object_statement(tokens)
    if tokens[:2] == ("ALTER", "TABLE"):
        return _alter_table(tokens)
    return IdempotencyCheck("ok")


def waiver_reason(comments: tuple[str, ...]) -> str | None:
    """None means absent; an empty string means a malformed, reasonless waiver."""
    reasons = []
    for comment in comments:
        match = _WAIVER.fullmatch(comment.strip())
        if match:
            reason = (match.group(1) or "").strip()
            if not reason:
                return ""
            reasons.append(reason)
    return "; ".join(reasons) if reasons else None


def _tokens(sql: str) -> tuple[str, ...]:
    tokens = []
    index = 0
    while index < len(sql):
        end = _skip_quoted_or_comment(sql, index)
        if end is not None:
            if sql[index] in "'\"`$":
                tokens.append("<QUOTED>")
            index = end
            continue
        if sql[index] == "{":
            end = sql.find("}", index + 1)
            if end != -1:
                tokens.append("<PLACEHOLDER>")
                index = end + 1
                continue
        word = _WORD.match(sql, index)
        if word:
            tokens.append(word.group().upper())
            index = word.end()
            continue
        if sql[index] in "(),[]":
            tokens.append(sql[index])
        index += 1
    return tuple(tokens)


def _object_statement(tokens: tuple[str, ...]) -> IdempotencyCheck:
    operation = tokens[0]
    index = 1
    if operation == "CREATE" and tokens[index : index + 2] == ("OR", "REPLACE"):
        return IdempotencyCheck("ok")
    if tokens[index : index + 1] == ("TEMPORARY",):
        index += 1
    if operation == "ATTACH" and "PARTITION" in tokens and "FROM" in tokens:
        return IdempotencyCheck("waiver")
    for kind in _OBJECTS:
        if tokens[index : index + len(kind)] == kind:
            clause = (
                ("IF", "NOT", "EXISTS") if operation in ("CREATE", "ATTACH") else ("IF", "EXISTS")
            )
            return _require_clause(tokens[index + len(kind) :], clause)
    return IdempotencyCheck("ok")


def _alter_table(tokens: tuple[str, ...]) -> IdempotencyCheck:
    first = next(
        (index for index in range(2, len(tokens)) if tokens[index] in _ACTIONS), len(tokens)
    )
    checks = [_alter_action(action) for action in _action_clauses(tokens[first:])]
    if any(check.status == "waiver" for check in checks):
        return IdempotencyCheck("waiver")
    fixes = dict.fromkeys(check.suggestion for check in checks if check.status == "fix")
    return IdempotencyCheck("fix", "; ".join(fixes)) if fixes else IdempotencyCheck("ok")


def _action_clauses(tokens: tuple[str, ...]) -> list[tuple[str, ...]]:
    actions = []
    start = depth = 0
    for index, token in enumerate(tokens):
        if token in ("(", "["):
            depth += 1
        elif token in (")", "]"):
            depth -= 1
        elif token == "," and depth == 0:
            actions.append(tokens[start:index])
            start = index + 1
    actions.append(tokens[start:])
    return actions


def _alter_action(tokens: tuple[str, ...]) -> IdempotencyCheck:
    if not tokens:
        return IdempotencyCheck("ok")
    operation = tokens[0]
    if operation in ("UPDATE", "DELETE"):
        return IdempotencyCheck("waiver")
    if tokens[:2] == ("ATTACH", "PARTITION") and "FROM" in tokens:
        return IdempotencyCheck("waiver")
    if tokens[:2] == ("MOVE", "PARTITION") and any(
        tokens[index : index + 2] == ("TO", "TABLE") for index in range(2, len(tokens))
    ):
        return IdempotencyCheck("waiver")
    if tokens[:2] == ("RENAME", "COLUMN"):
        return _require_clause(tokens[2:], ("IF", "EXISTS"))
    if operation in ("ADD", "DROP") and len(tokens) > 1:
        if tokens[1] not in ("PART", "PARTITION", "DETACHED", "TTL", "STATISTICS", "STATISTIC"):
            index = 2 if tokens[1] in ("COLUMN", "INDEX", "PROJECTION", "CONSTRAINT") else 1
            clause = ("IF", "NOT", "EXISTS") if operation == "ADD" else ("IF", "EXISTS")
            return _require_clause(tokens[index:], clause)
    if operation in ("ATTACH", "DETACH"):
        clause = ("IF", "NOT", "EXISTS") if operation == "ATTACH" else ("IF", "EXISTS")
        return _require_clause(tokens[2:], clause)
    return IdempotencyCheck("ok")


def _require_clause(tokens: tuple[str, ...], clause: tuple[str, ...]) -> IdempotencyCheck:
    if tokens[: len(clause)] == clause:
        return IdempotencyCheck("ok")
    return IdempotencyCheck(
        "fix", "Use " + " ".join(clause) + " (or an in-file waiver where unsupported)"
    )
