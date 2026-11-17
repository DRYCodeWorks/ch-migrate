"""The built-in grant sets for the migration role, shipped as `profiles/<name>.yaml` (2.4).

- `core`: what the migration role needs in a new project, with no grant option.
- `legacy`: 0.8's migration role exactly, one entry per statement of `build_bootstrap_sql`.

Profiles use access.yaml's grant syntax. They keep 0.8's privilege spelling, such as
`DELETE`, so legacy mode renders 0.8's statements as 0.8 wrote them.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from ch_migrate.access.blocks import PROFILE_NAMES
from ch_migrate.access.model import Profile
from ch_migrate.access.privileges import PrivilegeTree
from ch_migrate.access.reading import Reader

PROFILE_DIR = Path(__file__).with_name("profiles")


def load_profiles(values: dict[str, str], tree: PrivilegeTree) -> tuple[dict[str, Profile], Reader]:
    """Every built-in profile, rendered with `values`, and the reader holding its problems."""
    reader = Reader("", values, tree, canonical=False)
    profiles = {}
    for name in PROFILE_NAMES:
        reader.file = f"profiles/{name}.yaml (built in)"
        document = yaml.safe_load((PROFILE_DIR / f"{name}.yaml").read_text())
        fields = reader.mapping(document, "", ("grants",)) or {}
        profiles[name] = Profile(name, reader.grants(fields.get("grants", []), "grants"))
    return profiles, reader
