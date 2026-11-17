"""The grant syntax: `<privileges> ON <target> [WITH GRANT OPTION]`, in ClickHouse's own words.

A target is `*.*`, `<db>.*` or `<db>.<table>`. `READ` and `WRITE` also take a data source, as
in `READ ON REMOTE`. Column grants and prefix targets (`db.t*`) aren't in v1. Names are
checked offline against the bundled 26.3 list: an alias becomes its canonical name, and an
unknown name is a warning, because `access plan` checks names against the live server.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from ch_migrate.access.model import Grant, Target
from ch_migrate.access.privileges import PrivilegeTree
from ch_migrate.bootstrap import validate_identifier

# The privileges that take a data source as their target.
SOURCE_PRIVILEGES = frozenset({"READ", "WRITE"})

_GRANT = re.compile(
    r"^(?P<privileges>.+?)\s+ON\s+(?P<target>\S+)(?P<option>\s+WITH\s+GRANT\s+OPTION)?$",
    re.IGNORECASE,
)
# What a privilege name may hold, so nothing else reaches the rendered SQL.
_PRIVILEGE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_ ]*$")
# Statement words that belong elsewhere in access.yaml, and why.
_MISPLACED = (
    (re.compile(r"^GRANT\s", re.I), "write it without the leading GRANT"),
    (
        re.compile(r"\bCURRENT\s+GRANTS\b", re.I),
        "write a best-effort grant as {grant: ..., best_effort: true}",
    ),
    (re.compile(r"\sTO\s", re.I), "drop the TO clause: it goes to the user or role it's under"),
    (re.compile(r"\sWITH\s+(REPLACE|ADMIN)\s+OPTION\b", re.I), "only WITH GRANT OPTION is allowed"),
)
_TARGET_FORMS = "write `*.*`, `<db>.*`, `<db>.<table>` or a source such as `REMOTE`"
_SCOPE_WORDS = {"database": "a database (`<db>.*`)", "table": "a table", "global": "`*.*`"}


class GrantSyntaxError(ValueError):
    """A grant that doesn't parse. The message says why, in words for the person who wrote it."""


@dataclass(frozen=True)
class CheckedGrant:
    """A grant with canonical names, and what the offline check found."""

    grant: Grant
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


def parse_grant(text: str, *, best_effort: bool = False) -> Grant:
    """Parse one grant as written in access.yaml or a profile; names are checked later."""
    text = " ".join(text.split())
    if not text:
        raise GrantSyntaxError("the grant is empty")
    for pattern, reason in _MISPLACED:
        if pattern.search(text):
            raise GrantSyntaxError(reason)
    match = _GRANT.match(text)
    if not match:
        raise GrantSyntaxError("expected `<privileges> ON <target> [WITH GRANT OPTION]`")
    return Grant(
        _privileges(match["privileges"]),
        _target(match["target"]),
        grant_option=bool(match["option"]),
        best_effort=best_effort,
    )


def check_grant(grant: Grant, tree: PrivilegeTree) -> CheckedGrant:
    """Rewrite aliases to canonical names and check the names against the tree."""
    names = []
    warnings = []
    for name in grant.privileges:
        canonical = tree.canonical(name)
        names.append(canonical or name)
        if canonical is None:
            warnings.append(_unknown(name))
    privileges = tuple(names)
    errors = _source_errors(replace(grant, privileges=privileges), tree)
    if errors:
        return CheckedGrant(grant, tuple(errors), tuple(warnings))
    target = grant.target
    if target.source is not None:
        source = tree.source(target.source)
        if source is None:
            warnings.append(f"the bundled 26.3 list has no source named {target.source}")
        target = Target(source=source or target.source)
    for name in privileges:
        if name in tree.levels and not tree.flags(name, target.scope):
            warnings.append(_applies_nowhere(name, target))
    checked = replace(grant, privileges=privileges, target=target)
    return CheckedGrant(checked, (), tuple(warnings))


def _privileges(text: str) -> tuple[str, ...]:
    if "(" in text:
        raise GrantSyntaxError("column grants, such as SELECT(id), aren't supported in v1")
    names = tuple(" ".join(name.split()) for name in text.split(","))
    if not all(names):
        raise GrantSyntaxError("a privilege in the list is empty")
    for name in names:
        if not _PRIVILEGE_NAME.match(name):
            raise GrantSyntaxError(
                f"`{name}` isn't a privilege name: use letters, digits, underscores and spaces"
            )
    return names


def _target(text: str) -> Target:
    if text == "*.*":
        return Target()
    if "." not in text:
        if "*" in text:
            raise GrantSyntaxError(f"`{text}` isn't a target: {_TARGET_FORMS}")
        return Target(source=_identifier(text, "source name"))
    database, table = text.split(".", 1)
    if database == "*":
        raise GrantSyntaxError(f"`{text}` isn't a target: {_TARGET_FORMS}")
    if "*" in database or ("*" in table and table != "*"):
        raise GrantSyntaxError(f"prefix targets such as `{text}` aren't supported in v1")
    database = _identifier(database, "database name")
    return Target(database, None if table == "*" else _identifier(table, "table name"))


def _source_errors(grant: Grant, tree: PrivilegeTree) -> list[str]:
    """The old source spelling, and READ and WRITE on anything but a source or `*.*`."""
    errors = []
    alone = len(grant.privileges) == 1
    for name in grant.privileges:
        new = tree.new_source_spelling(name)
        if new is not None:
            subject, where = ("this is", "") if alone else (f"{name} is", " in an entry of its own")
            spelling = f"`READ, WRITE ON {new}`"
            errors.append(f"{subject} the old spelling of a source grant: write {spelling}{where}")
    reads = sorted(SOURCE_PRIVILEGES.intersection(grant.privileges))
    if grant.target.scope == "source" and set(grant.privileges) - SOURCE_PRIVILEGES:
        errors.append(
            "only READ and WRITE take a source as their target; a table is `<db>.<table>`"
        )
    elif reads and grant.target.scope in ("database", "table"):
        verb = "takes" if len(reads) == 1 else "take"
        errors.append(
            f"{' and '.join(reads)} {verb} a source, as in `READ ON REMOTE`, or `*.*` for all"
        )
    return errors


def _identifier(name: str, label: str) -> str:
    try:
        return validate_identifier(name, label)
    except ValueError as error:
        raise GrantSyntaxError(str(error)) from None


def _unknown(name: str) -> str:
    return f"ClickHouse 26.3 has no privilege named {name}; `access plan` checks it on the server"


def _applies_nowhere(name: str, target: Target) -> str:
    where = _SCOPE_WORDS.get(target.scope, "a source")
    return f"in ClickHouse 26.3, {name} can't be granted on {where}, so the server rejects it"
