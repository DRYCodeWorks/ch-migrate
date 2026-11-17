"""Read and validate access.yaml for one environment, or build legacy mode without one.

`validate_access` is what `access validate` runs. It never connects to a server and never
resolves a secret. It checks the built-in profiles too, and collects every problem:
- errors, which make the file invalid;
- warnings, such as a privilege name the bundled 26.3 list doesn't know.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ch_migrate.access.blocks import BLOCK_KEYS, Block, Entry, merge, read_block, role, user
from ch_migrate.access.document import parse_document
from ch_migrate.access.legacy import legacy_spec
from ch_migrate.access.model import AccessSpec, MigrationIdentity, Profile
from ch_migrate.access.privileges import bundled_tree
from ch_migrate.access.profiles import load_profiles
from ch_migrate.access.reading import Problem, Reader, child, describe, yaml_problem
from ch_migrate.access.rights import one_direction
from ch_migrate.access.settings import ProjectSettings

ACCESS_FILE = "access.yaml"
# An access.yaml that declares nothing would read as `core` with nothing declared, though a
# project that has one empty, or one `access show ENV > access.yaml` emptied before it ran,
# means legacy mode or the file it had.
EMPTY_ACCESS_FILE = (
    "access.yaml is empty. Delete it to keep legacy mode, or declare migration_role in it. "
    "`access show ENV > access.yaml` empties the file before `show` runs: write to another "
    "file and review it."
)
TOP_KEYS = (*BLOCK_KEYS, "environments")


@dataclass(frozen=True)
class Validation:
    """The declaration, unless there were errors, and every problem found."""

    spec: AccessSpec | None
    errors: tuple[Problem, ...] = ()
    warnings: tuple[Problem, ...] = ()


def validate_access(settings: ProjectSettings, root: Path) -> Validation:
    """Validate `root/access.yaml` for one environment; without one, build legacy mode."""
    profiles, profile_reader = load_profiles(settings.placeholders(), bundled_tree())
    path = root / ACCESS_FILE
    if path.exists():
        spec, reader = read_access_file(path, settings, profiles)
    else:
        spec, reader = legacy_spec(settings, profiles["legacy"], bundled_tree())
    errors = (*profile_reader.errors, *reader.errors)
    warnings = (*profile_reader.warnings, *reader.warnings)
    return Validation(None if errors else spec, errors, warnings)


def read_access_file(
    path: Path, settings: ProjectSettings, profiles: dict[str, Profile]
) -> tuple[AccessSpec, Reader]:
    """Read access.yaml, merge the environment's block over the base, and check the result."""
    reader = Reader(ACCESS_FILE, settings.placeholders(), bundled_tree())
    top = reader.mapping(_load_yaml(path, reader), "", TOP_KEYS) or {}
    base = read_block(reader, top, "")
    merged = merge(base, _environment_block(reader, top.get("environments"), settings))
    _check_identities(reader, merged, settings)
    _check_memberships(reader, merged)
    _check_passwords(reader, merged)
    _check_source_directions(reader, merged)
    return _spec(merged, settings, profiles), reader


def declares_access(root: Path) -> bool:
    """Whether `root` has an access.yaml; False without one, for legacy mode.

    One that is empty, or holds only comments, raises `ValueError` with `validate`'s message,
    so a migration helper decides the same way the access commands do.
    """
    path = root / ACCESS_FILE
    if not path.exists():
        return False
    try:
        document = parse_document(path.read_text())
    except yaml.YAMLError as error:
        raise ValueError(f"{ACCESS_FILE} can't be read: {error}") from None
    if document.data is None:
        raise ValueError(EMPTY_ACCESS_FILE)
    return True


def _load_yaml(path: Path, reader: Reader) -> Any:
    """The file as `yaml.safe_load` reads it; each key written twice in a mapping is an error,
    and so is a file that holds nothing, or only comments."""
    try:
        document = parse_document(path.read_text())
    except yaml.YAMLError as error:
        reader.error(*yaml_problem(error))
        return None
    if document.data is None:
        reader.error("", EMPTY_ACCESS_FILE)
    for duplicate in document.duplicates:
        reader.error(duplicate.path, duplicate.message)
    return document.data


def _environment_block(reader: Reader, value: Any, settings: ProjectSettings) -> Block:
    """Check every `environments.<env>` block, and return the one for this environment."""
    if value is None:
        return Block()
    if not isinstance(value, dict):
        reader.error("environments", f"expected a mapping of environments, found {describe(value)}")
        return Block()
    selected = Block()
    for name, body in value.items():
        path = child("environments", str(name))
        if str(name) not in settings.environments:
            reader.error(path, f"config.yaml has no environment named {name}")
        block = read_block(reader, reader.mapping(body, path, BLOCK_KEYS) or {}, path)
        if str(name) == settings.environment:
            selected = block
    return selected


def _check_identities(reader: Reader, block: Block, settings: ProjectSettings) -> None:
    """The migration identity is the profile's (2.2), and the admin is config.yaml's (2.1)."""
    if settings.migration_role in block.roles:
        path = block.roles[settings.migration_role].path
        reader.error(path, "this is the migration role: add its grants under migration_role.grants")
    reserved = {
        settings.migration_user: "this is the migration user, which `bootstrap` manages",
        settings.admin_user: "this is config.yaml's admin_user, which ch-migrate never changes",
    }
    for name, reason in reserved.items():
        if name in block.users:
            reader.error(block.users[name].path, reason)


def _check_memberships(reader: Reader, block: Block) -> None:
    """Default roles are among the user's roles, and nothing holds a role declared absent."""
    absent = {name for name, entry in block.roles.items() if entry.get("absent")}
    for entry in _present(block):
        held = entry.get("roles", ())
        for name in absent.intersection(held):
            reader.error(entry.where("roles"), f"{name} is declared absent, so nothing can hold it")
        defaults = entry.get("default_roles")
        extra = [name for name in defaults or () if name not in held]
        if extra:
            verb = "isn't" if len(extra) == 1 else "aren't"
            reader.error(entry.where("default_roles"), f"{', '.join(extra)} {verb} in roles")


def _check_passwords(reader: Reader, block: Block) -> None:
    for entry in block.users.values():
        if not entry.get("absent") and "password" not in entry.fields:
            reader.warn(
                entry.path,
                "no password, so it is never created and must already exist. "
                "Set `password: {secret: NAME}` to have it created.",
            )


def _check_source_directions(reader: Reader, block: Block) -> None:
    """`READ` without `WRITE` on a source, or the reverse, holds only with a server setting.

    The built-in profiles aren't checked: what they declare isn't the user's to change.
    """
    entries = _present(block)
    if block.migration:
        entries.insert(0, block.migration)
    for entry in entries:
        for grant in one_direction(entry.get("grants", ())):
            [direction] = grant.privileges
            reader.warn(
                entry.where("grants"),
                f"`{grant}` gives {direction} alone, which the server keeps only with "
                "access_control_improvements.enable_read_write_grants on. With it off, "
                f"ClickHouse's default, it holds READ and WRITE on {grant.target} together, "
                "and `plan` compares them as one grant.",
            )


def _spec(block: Block, settings: ProjectSettings, profiles: dict[str, Profile]) -> AccessSpec:
    migration = block.migration or Entry("migration_role")
    identity = MigrationIdentity(
        settings.migration_role,
        settings.migration_user,
        profiles[migration.get("profile", "core")],
        grants=migration.get("grants", ()),
    )
    return AccessSpec(
        settings.environment,
        identity,
        roles=tuple(role(name, entry) for name, entry in block.roles.items()),
        users=tuple(user(name, entry) for name, entry in block.users.items()),
    )


def _present(block: Block) -> list[Entry]:
    entries = [*block.roles.values(), *block.users.values()]
    return [entry for entry in entries if not entry.get("absent")]
