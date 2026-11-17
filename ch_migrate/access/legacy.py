"""Legacy mode: a project with no access.yaml gets what 0.8's `bootstrap` creates (2.4).

The migration role holds the `legacy` profile. Each of config.yaml's `mcp_user_name` and
`dict_reader_name` adds the role and user that `legacy_mode.yaml` declares, as 0.8 does.
0.8 also needs that user's password; validation never reads one, so the name alone counts.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from ch_migrate.access.blocks import Block, merge, read_block, role, user
from ch_migrate.access.model import AccessSpec, MigrationIdentity, Profile
from ch_migrate.access.privileges import PrivilegeTree
from ch_migrate.access.reading import Reader
from ch_migrate.access.settings import ProjectSettings

LEGACY_MODE_FILE = Path(__file__).with_name("legacy_mode.yaml")
# The config.yaml keys that each add a role and a user in legacy mode.
OPTIONAL_USERS = ("mcp_user_name", "dict_reader_name")
# The secret holding each optional user's password. 0.8 made the user only when it was set.
OPTIONAL_PASSWORDS = {"mcp_user_name": "mcp_password", "dict_reader_name": "dict_reader_password"}


def legacy_spec(
    settings: ProjectSettings, profile: Profile, tree: PrivilegeTree
) -> tuple[AccessSpec, Reader]:
    """The legacy-mode declaration for one environment, and the reader holding its problems."""
    names = {key: getattr(settings, key) for key in OPTIONAL_USERS if getattr(settings, key)}
    values = {**settings.placeholders(), **names}
    reader = Reader(f"{LEGACY_MODE_FILE.name} (built in)", values, tree, canonical=False)
    sections = yaml.safe_load(LEGACY_MODE_FILE.read_text())
    block = Block()
    for key in names:
        section = reader.mapping(sections[key], key, ("roles", "users")) or {}
        block = merge(block, read_block(reader, section, key))
    spec = AccessSpec(
        settings.environment,
        MigrationIdentity(settings.migration_role, settings.migration_user, profile),
        roles=tuple(role(name, entry) for name, entry in block.roles.items()),
        users=tuple(user(name, entry) for name, entry in block.users.items()),
        legacy=True,
    )
    return spec, reader
