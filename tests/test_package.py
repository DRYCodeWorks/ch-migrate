"""Tests for package metadata."""

from importlib.metadata import version

from click.testing import CliRunner

import ch_migrate
from ch_migrate.cli import main


def test_version_matches_distribution_metadata():
    # __version__ is the single source hatch builds from; the installed
    # distribution must report the same number.
    assert ch_migrate.__version__ == version("ch-migrate-cli")


def test_version_flag_prints_the_package_version():
    # The import name differs from the distribution name, which click cannot infer.
    result = CliRunner().invoke(main, ["--version"])
    assert result.exit_code == 0, result.output
    assert result.output == f"ch-migrate, version {ch_migrate.__version__}\n"
