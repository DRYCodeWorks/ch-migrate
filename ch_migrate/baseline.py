"""A committed script-head baseline exempts existing migrations in every environment."""

from __future__ import annotations

from collections.abc import MutableMapping
from io import StringIO
from pathlib import Path

from ruamel.yaml import YAML

from ch_migrate.rebase import RevisionGraph, build_revision_graph


def normalize_baseline(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, (list, tuple)) or any(
        not isinstance(item, str) or not item.strip() for item in values
    ):
        raise ValueError("lint.gate_baseline must be a revision ID or a list of revision IDs")
    return tuple(values)


def baseline_exemptions(graph: RevisionGraph, baseline: tuple[str, ...]) -> set[str]:
    unknown = set(baseline) - graph.migrations.keys()
    if unknown:
        raise ValueError(
            "Gate baseline revisions are missing locally: " + ", ".join(sorted(unknown))
        )
    exempt: set[str] = set()
    for head in baseline:
        exempt.update(graph.walk_to_root(head))
    return exempt


def record_baseline(project_root: Path) -> list[str]:
    """Update just the baseline value while round-tripping YAML comments and style."""
    heads = sorted(build_revision_graph(project_root / "migrations" / "versions").heads())
    path = project_root / "config.yaml"
    original = path.read_text()
    yaml = YAML()
    yaml.preserve_quotes = True
    config = yaml.load(original)
    if not isinstance(config, MutableMapping):
        raise ValueError("config.yaml must be a mapping")
    lint = config.setdefault("lint", {})
    if not isinstance(lint, MutableMapping):
        raise ValueError("config.yaml lint must be a mapping")
    lint["gate_baseline"] = heads[0] if len(heads) == 1 else heads
    output = StringIO()
    yaml.dump(config, output)
    if output.getvalue() != original:
        path.write_text(output.getvalue())
    return heads
