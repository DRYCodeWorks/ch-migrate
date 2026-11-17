"""Privilege names offline: the bundled list taken from ClickHouse 26.3's `system.privileges`.

`access validate` has no server, so it resolves privilege names against this list. An unknown
name is a warning, never an error: `access plan` checks names against the live server.

A privilege with a `level` carries a flag of its own; one without is a group. A privilege
means its own flag plus its descendants' flags, keeping only the levels the target's scope
allows. `system.privileges` doesn't say which privileges are data sources, so the list also
records the old source spellings (`REMOTE ON *.*`), found by granting each one on a scratch
role. `scripts/bundle_privileges.py` writes the list; a test pins it.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping

BUNDLED_LIST = Path(__file__).with_name("privileges_26_3.json")

# Levels a flag may have and still apply to a database target, or to a table target.
_DATABASE_LEVELS = frozenset({"DATABASE", "TABLE", "DICTIONARY", "VIEW", "COLUMN"})
_TABLE_LEVELS = frozenset({"TABLE", "DICTIONARY", "VIEW", "COLUMN"})


@dataclass(frozen=True)
class Privilege:
    """One row of `system.privileges`."""

    name: str
    aliases: tuple[str, ...]
    level: str | None
    parent: str | None


class PrivilegeTree:
    """Privilege names, aliases, levels and groups, plus the old source spellings."""

    def __init__(self, privileges: Iterable[Privilege], source_spellings: Mapping[str, str]):
        self.levels: dict[str, str | None] = {}
        self.children: dict[str, list[str]] = defaultdict(list)
        # The privileges with no parent group, such as ALL.
        self.roots: list[str] = []
        self._names: dict[str, str] = {}
        for privilege in privileges:
            self.levels[privilege.name] = privilege.level
            if privilege.parent is None:
                self.roots.append(privilege.name)
            else:
                self.children[privilege.parent].append(privilege.name)
            for spelling in (privilege.name, *privilege.aliases):
                self._names[_spelling(spelling)] = privilege.name
        # An old spelling, such as REMOTE, and the source it means; "" means every source.
        self.source_spellings = dict(source_spellings)
        self._sources = {_spelling(s): s for s in self.source_spellings.values() if s}

    def canonical(self, privilege: str) -> str | None:
        """The stored name for a privilege or one of its aliases; None when unknown."""
        return self._names.get(_spelling(privilege))

    def source(self, name: str) -> str | None:
        """The stored spelling of a data source, such as `REMOTE`; None when unknown."""
        return self._sources.get(_spelling(name))

    def new_source_spelling(self, privilege: str) -> str | None:
        """For an old source spelling, the target that replaces it: `REMOTE`, or `*.*`."""
        if privilege not in self.source_spellings:
            return None
        return self.source_spellings[privilege] or "*.*"

    def flags(self, privilege: str, scope: str) -> frozenset[str]:
        """The flags a known privilege grants on a scope: global, database, table or source."""
        flags = {privilege} if _level_allowed(self.levels[privilege], scope) else set()
        for child in self.children[privilege]:
            flags.update(self.flags(child, scope))
        return frozenset(flags)


def bundled_tree() -> PrivilegeTree:
    """The tree from the bundled 26.3 list."""
    return tree_from_document(_bundled_document())


def tree_from_document(document: Mapping[str, Any]) -> PrivilegeTree:
    """Build a tree from a privilege list in the bundled file's shape."""
    privileges = (
        Privilege(row["privilege"], tuple(row["aliases"]), row["level"], row["parent_group"])
        for row in document["privileges"]
    )
    return PrivilegeTree(privileges, document["source_spellings"])


@lru_cache(maxsize=1)
def _bundled_document() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(BUNDLED_LIST.read_text())
    return document


def _spelling(name: str) -> str:
    return " ".join(name.upper().split())


def _level_allowed(level: str | None, scope: str) -> bool:
    if level is None:
        return False
    if scope == "global":
        return True
    if scope == "source":
        return level == "SOURCE"
    return level in (_DATABASE_LEVELS if scope == "database" else _TABLE_LEVELS)
