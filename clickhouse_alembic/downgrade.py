"""Irreversible migrations, and working out what a `down` would revert.

A migration declares itself irreversible with a module-level reason:

    irreversible = "Drops column legacy_id; its data cannot be restored."

`ch-migrate down` refuses before running anything when the range it would
revert includes such a migration. The migration's own downgrade() raises
IrreversibleMigration as well, for when Alembic is run directly.
"""

from __future__ import annotations

import re

from clickhouse_alembic.rebase import _MISSING, RevisionGraph, _literal_assignment

_RELATIVE = re.compile(r"^-(\d+)$")


class IrreversibleMigration(RuntimeError):
    """Raised by the downgrade() of a migration that cannot be reversed."""

    def __init__(self, revision: str, reason: str) -> None:
        super().__init__(f"Migration {revision} is irreversible: {reason}")
        self.revision = revision
        self.reason = reason


def irreversible_reason(graph: RevisionGraph, revision: str) -> str | None:
    """The migration's irreversible reason, or None if it can be reversed."""
    migration = graph.migrations.get(revision)
    if migration is None:
        return None
    value = _literal_assignment(migration.path.read_text(), "irreversible")
    if value is _MISSING or value is None or value is False:
        return None
    return value if isinstance(value, str) and value.strip() else "(no reason given)"


def revisions_to_revert(graph: RevisionGraph, heads: set[str], target: str) -> list[str] | None:
    """Revisions a downgrade from `heads` to `target` would revert, newest first.

    Understands `base`, `-N` and a full or unique-prefix revision id. Returns
    None for any other form, or when the answer depends on a merge point; the
    caller then relies on each migration's own downgrade() to refuse.
    """
    if target == "base":
        return _applied_newest_first(graph, heads, keep=set())
    relative = _RELATIVE.match(target)
    if relative:
        return _walk_back(graph, heads, int(relative.group(1)))
    revision = _resolve(graph, target)
    if revision is None or revision not in _applied(graph, heads):
        return None
    return _applied_newest_first(graph, heads, keep=_applied(graph, {revision}))


def _applied(graph: RevisionGraph, heads: set[str]) -> set[str]:
    applied: set[str] = set()
    for head in heads:
        applied.update(graph.walk_to_root(head))
    return applied


def _applied_newest_first(graph: RevisionGraph, heads: set[str], keep: set[str]) -> list[str]:
    """Applied revisions not in `keep`, ordered so children come before parents."""
    remaining = _applied(graph, heads) - keep
    ordered: list[str] = []
    while remaining:
        leaves = sorted(
            rev
            for rev in remaining
            if not any(child in remaining for child in graph.children.get(rev, []))
        )
        if not leaves:
            break  # a cycle; leave the rest to Alembic
        ordered.extend(leaves)
        remaining -= set(leaves)
    return ordered


def _walk_back(graph: RevisionGraph, heads: set[str], steps: int) -> list[str] | None:
    """Follow a single line of history back `steps` revisions."""
    if len(heads) != 1:
        return None
    current: str | None = next(iter(heads))
    reverted: list[str] = []
    for _ in range(steps):
        migration = graph.migrations.get(current) if current else None
        if migration is None or len(migration.down_revisions) > 1:
            return None
        reverted.append(migration.revision)
        current = migration.down_revisions[0] if migration.down_revisions else None
    return reverted


def _resolve(graph: RevisionGraph, target: str) -> str | None:
    """A full revision id, or the one revision the prefix matches."""
    if target in graph.migrations:
        return target
    matches = [rev for rev in graph.migrations if rev.startswith(target)]
    return matches[0] if len(matches) == 1 else None
