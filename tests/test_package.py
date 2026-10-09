"""Tests for package metadata."""

from importlib.metadata import version

import ch_migrate


def test_version_matches_distribution_metadata():
    # __version__ is the single source hatch builds from; the installed
    # distribution must report the same number.
    assert ch_migrate.__version__ == version("ch-migrate-cli")
