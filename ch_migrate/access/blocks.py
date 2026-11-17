"""Blocks of access.yaml: `migration_role`, `roles` and `users`, read and merged.

The file's base and each `environments.<env>` block are read on their own, so every problem
names the key path where it was written. An environment block then deep-merges over the base:
mappings merge, lists replace. Each field remembers the path of the block that set it last.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ch_migrate.access.model import Role, User
from ch_migrate.access.reading import Reader, child, describe

BLOCK_KEYS = ("migration_role", "roles", "users")
MIGRATION_KEYS = ("profile", "grants")
ROLE_KEYS = ("grants", "roles", "absent")
USER_KEYS = ("password", "roles", "default_roles", "grants", "absent")
PROFILE_NAMES = ("core", "legacy")


@dataclass
class Entry:
    """One declared user or role, or `migration_role`: the fields set, and where."""

    path: str  # the key path of the block that declared it last
    fields: dict[str, Any] = field(default_factory=dict)
    paths: dict[str, str] = field(default_factory=dict)  # each field's key path

    def get(self, key: str, default: Any = None) -> Any:
        return self.fields.get(key, default)

    def where(self, key: str) -> str:
        """The key path of a field, or where it would go."""
        return self.paths.get(key, child(self.path, key))


@dataclass
class Block:
    """The base of the file, or one environment block, or the two merged."""

    migration: Entry | None = None
    roles: dict[str, Entry] = field(default_factory=dict)
    users: dict[str, Entry] = field(default_factory=dict)


def read_block(reader: Reader, block: dict[str, Any], path: str) -> Block:
    """Read the `migration_role`, `roles` and `users` keys of an already-checked mapping."""
    result = Block()
    if "migration_role" in block:
        section = _Section(child(path, "migration_role"), MIGRATION_KEYS)
        result.migration = _entry(reader, block["migration_role"], section)
    if "roles" in block:
        result.roles = _entries(reader, block["roles"], _Section(child(path, "roles"), ROLE_KEYS))
    if "users" in block:
        result.users = _entries(reader, block["users"], _Section(child(path, "users"), USER_KEYS))
    return result


def merge(base: Block, override: Block) -> Block:
    """Deep-merge an environment block over the base: mappings merge and lists replace."""
    migration = base.migration or override.migration
    if base.migration and override.migration:
        migration = _merged(base.migration, override.migration)
    return Block(
        migration, _merged_all(base.roles, override.roles), _merged_all(base.users, override.users)
    )


def role(name: str, entry: Entry) -> Role:
    return Role(
        name,
        grants=entry.get("grants", ()),
        roles=entry.get("roles", ()),
        absent=entry.get("absent", False),
    )


def user(name: str, entry: Entry) -> User:
    return User(
        name,
        password=entry.get("password"),
        grants=entry.get("grants", ()),
        roles=entry.get("roles", ()),
        default_roles=entry.get("default_roles"),
        absent=entry.get("absent", False),
    )


@dataclass(frozen=True)
class _Section:
    """Where a declaration sits, and the keys it may hold."""

    path: str
    keys: tuple[str, ...]


def _entries(reader: Reader, value: Any, section: _Section) -> dict[str, Entry]:
    """Declared users or roles by rendered name; `section.keys` are each one's keys."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        reader.error(section.path, f"expected a mapping of names, found {describe(value)}")
        return {}
    entries: dict[str, Entry] = {}
    for key, body in value.items():
        path = child(section.path, str(key))
        name = reader.name(key, path)
        if name is None:
            continue
        if name in entries:
            reader.error(path, f"declares {name} a second time")
            continue
        entries[name] = _entry(reader, body, _Section(path, section.keys))
    return entries


def _entry(reader: Reader, value: Any, section: _Section) -> Entry:
    fields = reader.mapping(value, section.path, section.keys) or {}
    entry = Entry(section.path)
    for key, item in fields.items():
        path = child(section.path, key)
        entry.fields[key] = _FIELD_READERS[key](reader, item, path)
        entry.paths[key] = path
    return entry


def _merged_all(base: dict[str, Entry], override: dict[str, Entry]) -> dict[str, Entry]:
    merged = dict(base)
    for name, entry in override.items():
        merged[name] = _merged(merged[name], entry) if name in merged else entry
    return merged


def _merged(base: Entry, override: Entry) -> Entry:
    return Entry(
        override.path,
        {**base.fields, **override.fields},
        {**base.paths, **override.paths},
    )


def _profile(reader: Reader, value: Any, path: str) -> str:
    if value not in PROFILE_NAMES:
        reader.error(path, f"unknown profile {value!r}; the profiles are core and legacy")
        return "core"
    return str(value)


_FIELD_READERS: dict[str, Callable[[Reader, Any, str], Any]] = {
    "grants": lambda reader, value, path: reader.grants(value, path),
    "roles": lambda reader, value, path: reader.names(value, path),
    "default_roles": lambda reader, value, path: reader.names(value, path),
    "absent": lambda reader, value, path: reader.flag(value, path),
    "password": lambda reader, value, path: reader.secret(value, path),
    "profile": _profile,
}
