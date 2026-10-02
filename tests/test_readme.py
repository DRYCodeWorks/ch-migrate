"""The documented entry path stays SQL-only and covers the installed CLI."""

import re
from pathlib import Path

from clickhouse_alembic.cli import main


def test_quick_start_requires_no_python():
    quick_start = _section("Quick start")
    assert not re.search(r"^```\s*python\b", quick_start, re.MULTILINE | re.IGNORECASE)
    assert not re.search(r"^\s*(?:import\s+\w|from\s+[\w.]+\s+import\b)", quick_start, re.MULTILINE)


def test_command_reference_covers_registered_commands():
    reference = _section("Command reference")
    documented = set(re.findall(r"^### `([^`]+)`", reference, re.MULTILINE))
    assert (
        set(main.commands) <= documented
    ), f"Undocumented commands: {set(main.commands) - documented}"


def _section(name):
    readme = (Path(__file__).parents[1] / "README.md").read_text()
    match = re.search(rf"^## {re.escape(name)}\n(.*?)(?=^## |\Z)", readme, re.MULTILINE | re.DOTALL)
    assert match is not None, f"Missing README section: {name}"
    return match.group(1)
