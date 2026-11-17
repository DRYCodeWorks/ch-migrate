"""`init` writes an access.yaml: the core profile, with commented examples (Decision 5)."""

import re

import yaml
from click.testing import CliRunner

from ch_migrate.cli import main

# A commented line of YAML in the examples: a key, or a list item, after `# `.
EXAMPLE_LINE = re.compile(r"^(\s*)# (?=\s*(?:[\w-]+:|- ))", re.MULTILINE)


def init(path):
    result = CliRunner().invoke(main, ["init", str(path), "--name", "shop"])
    assert result.exit_code == 0, result.output
    return result


def validate():
    return CliRunner().invoke(main, ["access", "validate", "dev"])


def test_init_writes_access_yaml_with_only_the_core_profile(tmp_path, monkeypatch):
    result = init(tmp_path)
    assert "Created access.yaml" in result.stdout
    assert yaml.safe_load((tmp_path / "access.yaml").read_text()) == {
        "migration_role": {"profile": "core"}
    }
    monkeypatch.chdir(tmp_path)
    checked = validate()
    assert checked.exit_code == 0, checked.output
    assert "the migration role shop_migration_role holds the core profile" in checked.stdout


def test_its_commented_examples_are_valid_once_uncommented(tmp_path, monkeypatch):
    init(tmp_path)
    path = tmp_path / "access.yaml"
    text = path.read_text()
    assert "# roles:" in text and "# users:" in text and "# environments:" in text
    path.write_text(EXAMPLE_LINE.sub(r"\1", text))
    monkeypatch.chdir(tmp_path)
    checked = validate()
    assert checked.exit_code == 0, checked.output
    assert "2 roles and 1 user declared" in checked.stdout


def test_init_keeps_an_existing_access_yaml(tmp_path):
    (tmp_path / "access.yaml").write_text("migration_role: {profile: legacy}\n")
    result = init(tmp_path)
    assert "Skipped access.yaml (already exists)" in result.stdout
    assert (tmp_path / "access.yaml").read_text() == "migration_role: {profile: legacy}\n"
