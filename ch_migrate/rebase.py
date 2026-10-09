"""Rebase dangling migration branches onto a new parent revision."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DownRevisions = tuple[str, ...]
_MISSING = object()


@dataclass
class Migration:
    """A parsed Alembic migration file."""

    path: Path
    revision: str
    down_revision: str | None
    down_revisions: DownRevisions = ()
    description: str | None = None
    create_date: str | None = None


@dataclass
class RevisionGraph:
    """Graph of migration revisions built from file parsing."""

    migrations: dict[str, Migration] = field(default_factory=dict)
    children: dict[str | None, list[str]] = field(default_factory=lambda: {None: []})

    def heads(self) -> list[str]:
        """Find revisions that have no children (tips of branches)."""
        has_children = {parent for parent, kids in self.children.items() if kids}
        return [rev for rev in self.migrations if rev not in has_children]

    def ancestors(self, revision: str) -> set[str]:
        """Return all ancestors of a revision (not including itself).

        Stops if a cycle is detected to prevent infinite loops.
        """
        result: set[str] = set()
        current = self.migrations.get(revision)
        stack = list(current.down_revisions) if current else []

        while stack:
            parent = stack.pop()
            if parent in result:
                continue
            result.add(parent)

            parent_migration = self.migrations.get(parent)
            if parent_migration:
                stack.extend(reversed(parent_migration.down_revisions))

        return result

    def walk_to_root(self, revision: str) -> list[str]:
        """Walk from revision back to root, returning reachable ancestors.

        Stops if a cycle is detected to prevent infinite loops.
        """
        chain: list[str] = []
        visited: set[str] = set()

        def visit(rev: str) -> None:
            if rev in visited:
                return
            visited.add(rev)
            chain.append(rev)

            migration = self.migrations.get(rev)
            if not migration:
                return
            for parent in migration.down_revisions:
                visit(parent)

        visit(revision)
        return chain


def _literal_assignment(content: str, name: str) -> Any:
    """Return a top-level assignment's literal value, or _MISSING."""
    try:
        module = ast.parse(content)
    except SyntaxError:
        return _MISSING

    for statement in module.body:
        value_node: ast.AST | None = None
        if isinstance(statement, ast.Assign):
            for target in statement.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    value_node = statement.value
                    break
        elif (
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and statement.target.id == name
        ):
            value_node = statement.value

        if value_node is None:
            continue

        try:
            return ast.literal_eval(value_node)
        except (SyntaxError, ValueError):
            return _MISSING

    return _MISSING


def _normalize_down_revisions(value: Any) -> DownRevisions:
    if value is _MISSING or value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        if all(isinstance(item, str) for item in value):
            return tuple(value)
    return ()


def parse_migration(path: Path) -> Migration | None:
    """Extract revision and down_revision from a migration file.

    Returns None if the file can't be parsed.
    """
    content = path.read_text()

    revision_value = _literal_assignment(content, "revision")
    if isinstance(revision_value, str):
        revision = revision_value
    else:
        rev_match = re.search(r"^revision\s*=\s*['\"](\w+)['\"]", content, re.MULTILINE)
        if not rev_match:
            return None
        revision = rev_match.group(1)

    down_revision_value = _literal_assignment(content, "down_revision")
    down_revisions = _normalize_down_revisions(down_revision_value)
    if down_revision_value is _MISSING:
        down_match = re.search(r"^down_revision\s*=\s*['\"](\w+)['\"]", content, re.MULTILINE)
        down_revisions = (down_match.group(1),) if down_match else ()

    # Extract description from first line of module docstring
    description = None
    desc_match = re.search(r'^"""(.+?)$', content, re.MULTILINE)
    if desc_match:
        description = desc_match.group(1).strip()

    # Extract create date
    create_date = None
    date_match = re.search(r"^Create Date:\s*(.+)$", content, re.MULTILINE)
    if date_match:
        create_date = date_match.group(1).strip()

    return Migration(
        path=path,
        revision=revision,
        down_revision=down_revisions[0] if len(down_revisions) == 1 else None,
        down_revisions=down_revisions,
        description=description,
        create_date=create_date,
    )


def build_revision_graph(versions_dir: Path) -> RevisionGraph:
    """Scan migration files and build a revision graph."""
    graph = RevisionGraph()

    for path in sorted(versions_dir.glob("*.py")):
        migration = parse_migration(path)
        if migration:
            graph.migrations[migration.revision] = migration
            parents: tuple[str | None, ...] = migration.down_revisions or (None,)
            for parent in parents:
                if parent not in graph.children:
                    graph.children[parent] = []
                graph.children[parent].append(migration.revision)

    return graph


def find_branch_roots(graph: RevisionGraph, onto: str) -> list[Migration]:
    """Find the root migration of each dangling branch.

    For each head that isn't `onto` (or a descendant of `onto`),
    walk back to find the first migration whose down_revision is
    an ancestor of `onto` (or is `onto` itself). That migration's
    down_revision is what gets rewritten.

    Returns the list of migrations that need their down_revision changed.
    """
    heads = graph.heads()
    onto_ancestors = graph.ancestors(onto) | {onto}
    roots = []

    for head in heads:
        if head == onto:
            continue

        # Walk back from this head
        chain = graph.walk_to_root(head)
        for rev_id in chain:
            migration = graph.migrations[rev_id]
            if set(migration.down_revisions) & onto_ancestors:
                roots.append(migration)
                break

    return roots


def rewrite_down_revision(path: Path, old_rev: str, new_rev: str) -> str:
    """Rewrite down_revision in a migration file.

    Updates both:
    - The `down_revision = '...'` variable assignment
    - The `Revises: ...` line in the docstring

    Returns the new file content (does not write to disk).
    """
    content = path.read_text()

    # Replace the variable assignment
    content = re.sub(
        r"(^down_revision\s*=\s*['\"])" + re.escape(old_rev) + r"(['\"])",
        r"\g<1>" + new_rev + r"\2",
        content,
        count=1,
        flags=re.MULTILINE,
    )

    # Replace the Revises: line in the docstring
    content = re.sub(
        r"(^Revises:\s*)" + re.escape(old_rev),
        r"\g<1>" + new_rev,
        content,
        count=1,
        flags=re.MULTILINE,
    )

    return content


@dataclass
class RebaseChange:
    """A planned change to a migration file."""

    migration: Migration
    old_down_revision: str
    new_down_revision: str


def plan_rebase(versions_dir: Path, onto: str) -> list[RebaseChange]:
    """Plan the rebase without making changes.

    Returns a list of changes that would be made.
    Raises ValueError if onto revision is not found or nothing to do.
    """
    graph = build_revision_graph(versions_dir)

    if onto not in graph.migrations:
        raise ValueError(f"Revision '{onto}' not found in migrations")

    heads = graph.heads()
    if len(heads) <= 1:
        raise ValueError("Only one head found — nothing to rebase")

    roots = find_branch_roots(graph, onto)
    if not roots:
        raise ValueError("No dangling branches found to rebase")

    changes: list[RebaseChange] = []
    for root in roots:
        if len(root.down_revisions) != 1:
            raise ValueError(
                f"Cannot safely rebase merge revision '{root.revision}' "
                f"with {len(root.down_revisions)} down revisions"
            )

        old_down_revision = root.down_revisions[0]
        if old_down_revision == onto:
            continue

        changes.append(
            RebaseChange(
                migration=root,
                old_down_revision=old_down_revision,
                new_down_revision=onto,
            )
        )

    return changes


def apply_rebase(changes: list[RebaseChange]) -> None:
    """Apply planned rebase changes to disk."""
    for change in changes:
        new_content = rewrite_down_revision(
            change.migration.path,
            change.old_down_revision,
            change.new_down_revision,
        )
        change.migration.path.write_text(new_content)
