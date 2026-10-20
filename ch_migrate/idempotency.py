"""Statement-level idempotency classification, without evaluating SQL literals.

Each finding says how to make the statement safe to run twice, quoting the
names from the statement itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Literal

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
_MARKS = "(),[]."
_WAIVER = re.compile(r"^ch-migrate:\s*allow-non-idempotent(?:\s+(.*))?$", re.IGNORECASE)
_WAIVE = "waive it with -- ch-migrate: allow-non-idempotent <reason> on the line above"
_DELETE = (
    f"A retry deletes whatever still matches the WHERE, usually nothing. If that's safe, {_WAIVE}"
)
_MOVE = (
    "A retry moves whatever has reached the partition since, usually nothing. "
    f"If that's safe, {_WAIVE}"
)
_PART = (
    "A retry fails because the part has already moved, and parts have no IF EXISTS. "
    f"Use the whole PARTITION, which a retry skips, or {_WAIVE}"
)


@dataclass(frozen=True)
class IdempotencyCheck:
    status: Literal["ok", "fix", "waiver"]
    suggestion: str = ""


def classify_idempotency(sql: str) -> IdempotencyCheck:
    tokens = _tokens(sql)
    if not tokens:
        return IdempotencyCheck("ok")
    first = tokens[0]
    if first == "INSERT":
        return IdempotencyCheck("waiver", _insert_advice(tokens))
    if first == "UPDATE":
        return IdempotencyCheck("waiver", _update_advice(_after(tokens, "SET")))
    if tokens[:2] == ("DELETE", "FROM"):
        return IdempotencyCheck("waiver", _DELETE)
    if tokens[:2] == ("EXCHANGE", "TABLES"):
        return IdempotencyCheck("waiver", _exchange_advice(tokens[2:]))
    if first == "RENAME" and len(tokens) > 1 and tokens[1] in ("TABLE", "DICTIONARY", "DATABASE"):
        return _require_clause(tokens[2:], ("IF", "EXISTS"), tokens[:2], tokens[1].lower())
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


class _Token(str):
    """An uppercased word or mark that remembers where it sits in the statement."""

    source: str
    start: int
    end: int

    def __new__(cls, value: str, source: str, start: int, end: int) -> _Token:
        token = super().__new__(cls, value)
        token.source, token.start, token.end = source, start, end
        return token


def _tokens(sql: str) -> tuple[_Token, ...]:
    tokens = []
    index = 0
    while index < len(sql):
        end = _skip_quoted_or_comment(sql, index)
        if end is not None:
            if sql[index] in "'\"`$":
                tokens.append(_Token("<QUOTED>", sql, index, end))
            index = end
            continue
        if sql[index] == "{":
            end = sql.find("}", index + 1)
            if end != -1:
                tokens.append(_Token("<PLACEHOLDER>", sql, index, end + 1))
                index = end + 1
                continue
        word = _WORD.match(sql, index)
        if word:
            tokens.append(_Token(word.group().upper(), sql, index, word.end()))
            index = word.end()
            continue
        if sql[index] in _MARKS:
            tokens.append(_Token(sql[index], sql, index, index + 1))
        index += 1
    return tuple(tokens)


def _object_statement(tokens: tuple[_Token, ...]) -> IdempotencyCheck:
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
            head = tokens[: index + len(kind)]
            return _require_clause(
                tokens[index + len(kind) :], clause, head, " ".join(kind).lower()
            )
    return IdempotencyCheck("ok")


def _alter_table(tokens: tuple[_Token, ...]) -> IdempotencyCheck:
    first = next(
        (index for index in range(2, len(tokens)) if tokens[index] in _ACTIONS),
        len(tokens),
    )
    checks = [_alter_action(action) for action in _action_clauses(tokens[first:])]
    waiver = next((check for check in checks if check.status == "waiver"), None)
    if waiver:
        return waiver
    fixes = list(dict.fromkeys(check.suggestion for check in checks if check.status == "fix"))
    if not fixes:
        return IdempotencyCheck("ok")
    return IdempotencyCheck(
        "fix", "; ".join([fixes[0], *(fix[0].lower() + fix[1:] for fix in fixes[1:])])
    )


def _action_clauses(tokens: tuple[_Token, ...]) -> list[tuple[_Token, ...]]:
    """ALTER actions split on top-level commas; UPDATE assignments run to WHERE."""
    return _split(tokens, hold=_in_update)


def _alter_action(tokens: tuple[_Token, ...]) -> IdempotencyCheck:
    if not tokens:
        return IdempotencyCheck("ok")
    operation = tokens[0]
    if operation == "UPDATE":
        return IdempotencyCheck("waiver", _update_advice(tokens[1:]))
    if operation == "DELETE":
        return IdempotencyCheck("waiver", _DELETE)
    if tokens[:2] == ("ATTACH", "PARTITION") and "FROM" in tokens:
        return IdempotencyCheck("waiver", _attach_from_advice(tokens))
    if tokens[:2] == ("MOVE", "PARTITION") and any(
        tokens[index : index + 2] == ("TO", "TABLE") for index in range(2, len(tokens))
    ):
        return IdempotencyCheck("waiver", _MOVE)
    if tokens[:2] == ("RENAME", "COLUMN"):
        return _require_clause(tokens[2:], ("IF", "EXISTS"), tokens[:2], "column")
    if operation in ("ADD", "DROP") and len(tokens) > 1:
        if tokens[1] not in (
            "PART",
            "PARTITION",
            "DETACHED",
            "TTL",
            "STATISTICS",
            "STATISTIC",
        ):
            index = 2 if tokens[1] in ("COLUMN", "INDEX", "PROJECTION", "CONSTRAINT") else 1
            clause = ("IF", "NOT", "EXISTS") if operation == "ADD" else ("IF", "EXISTS")
            head = tokens[:index] if index == 2 else (operation, "COLUMN")
            return _require_clause(tokens[index:], clause, head, head[1].lower())
    if operation in ("ATTACH", "DETACH"):
        return (
            IdempotencyCheck("waiver", _PART)
            if tokens[1:2] == ("PART",)
            else IdempotencyCheck("ok")
        )
    return IdempotencyCheck("ok")


def _require_clause(
    tokens: tuple[_Token, ...],
    clause: tuple[str, ...],
    head: tuple[str, ...],
    noun: str,
) -> IdempotencyCheck:
    if tokens[: len(clause)] == clause:
        return IdempotencyCheck("ok")
    name, used = _name(tokens)
    if name and tokens[used : used + 1] == ("TO",):
        name = f"{name} TO {_name(tokens[used + 1 :])[0]}"
    written = " ".join((*head, *clause, name)).strip()
    state = "existing" if "NOT" in clause else "missing"
    return IdempotencyCheck("fix", f"Write {written} so a retry skips the {state} {noun}")


def _insert_advice(tokens: tuple[_Token, ...]) -> str:
    rest = _after(tokens, "INTO")
    if rest[:1] == ("TABLE",):
        rest = rest[1:]
    target = "" if rest[:1] == ("FUNCTION",) else _name(rest)[0]
    if not target:
        return f"A retry inserts the rows again. Make it skip rows that are already there, then {_WAIVE}"
    return (
        f"A retry inserts the rows again. If nothing else writes to {target}, put "
        f"TRUNCATE TABLE {target} before it so a retry starts over, then {_WAIVE}"
    )


def _update_advice(assignments: tuple[_Token, ...]) -> str:
    assignments = assignments[: _index(assignments, "WHERE")]
    pairs = [(part[0], part[1:]) for part in _split(assignments) if part]
    targets = {target: target for target, _ in pairs}
    for target, reads in pairs:
        source = next((read for read in reads if read in targets), None)
        if source is None:
            continue
        column = _raw(target, target)
        detail = (
            f"{column} is computed from its old value"
            if source == target
            else f"{column} is computed from {_raw(targets[source], targets[source])}, "
            "which it also sets"
        )
        return (
            f"A retry runs the update again, and {detail}. Set fixed values, or narrow "
            f"the WHERE so updated rows no longer match, then {_WAIVE}"
        )
    return (
        "A retry runs the update again. It doesn't read the columns it sets, so a second run "
        f"writes the same values. If that's safe, {_WAIVE}"
    )


def _exchange_advice(tokens: tuple[_Token, ...]) -> str:
    first, used = _name(tokens)
    second = _name(tokens[used + 1 :])[0] if tokens[used : used + 1] == ("AND",) else ""
    tables = f"{first} and {second}" if first and second else "the tables"
    return (
        f"A retry swaps {tables} back. Make this the last statement in its file so nothing "
        f"after it can fail and force a retry, then {_WAIVE}"
    )


def _attach_from_advice(tokens: tuple[_Token, ...]) -> str:
    rewrite = "REPLACE " + _raw(tokens[1], tokens[-1])
    return (
        "A retry attaches the rows again. If the partition should end up a copy of the "
        f"source, write {rewrite} instead, which a retry repeats safely; otherwise {_WAIVE}"
    )


def _name(tokens: tuple[_Token, ...]) -> tuple[str, int]:
    """The dotted name at the start of tokens, as written, and how many tokens it spans."""
    if not tokens or tokens[0] in _MARKS or tokens[0] in ("IF", "ON", "AS", "TO", "AND"):
        return "", 0
    used = 1
    while tokens[used : used + 1] == (".",) and used + 1 < len(tokens):
        used += 2
    return _raw(tokens[0], tokens[used - 1]), used


def _raw(first: _Token, last: _Token) -> str:
    return first.source[first.start : last.end]


def _after(tokens: tuple[_Token, ...], keyword: str) -> tuple[_Token, ...]:
    return tokens[_index(tokens, keyword) + 1 :]


def _index(tokens: tuple[_Token, ...], keyword: str) -> int:
    return next((index for index, token in enumerate(tokens) if token == keyword), len(tokens))


def _split(
    tokens: tuple[_Token, ...],
    hold: Callable[[tuple[_Token, ...]], bool] = lambda part: False,
) -> list[tuple[_Token, ...]]:
    parts = []
    start = depth = 0
    for index, token in enumerate(tokens):
        if token in ("(", "["):
            depth += 1
        elif token in (")", "]"):
            depth -= 1
        elif token == "," and depth == 0 and not hold(tokens[start:index]):
            parts.append(tokens[start:index])
            start = index + 1
    parts.append(tokens[start:])
    return parts


def _in_update(action: tuple[_Token, ...]) -> bool:
    return action[:1] == ("UPDATE",) and "WHERE" not in action
