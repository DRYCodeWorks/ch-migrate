"""Tests for irreversible migrations and downgrade planning."""

from pathlib import Path
from typing import Optional, Tuple, Union

import pytest

from ch_migrate.downgrade import (
    IrreversibleMigration,
    irreversible_reason,
    revisions_to_revert,
)
from ch_migrate.rebase import build_revision_graph

DownRevision = Optional[Union[str, Tuple[str, ...]]]


class TestRevisionsToRevert:
    def test_minus_one(self, linear):
        assert revisions_to_revert(linear, {"dddd"}, "-1") == ["dddd"]

    def test_minus_n(self, linear):
        assert revisions_to_revert(linear, {"dddd"}, "-3") == ["dddd", "cccc", "bbbb"]

    def test_past_the_root_is_unknown(self, linear):
        assert revisions_to_revert(linear, {"bbbb"}, "-3") is None

    def test_target_revision(self, linear):
        assert revisions_to_revert(linear, {"dddd"}, "bbbb") == ["dddd", "cccc"]

    def test_target_prefix(self, linear):
        assert revisions_to_revert(linear, {"dddd"}, "bb") == ["dddd", "cccc"]

    def test_base(self, linear):
        assert revisions_to_revert(linear, {"dddd"}, "base") == ["dddd", "cccc", "bbbb", "aaaa"]

    def test_target_not_applied_is_unknown(self, linear):
        assert revisions_to_revert(linear, {"bbbb"}, "dddd") is None

    def test_unrecognised_form_is_unknown(self, linear):
        assert revisions_to_revert(linear, {"dddd"}, "heads") is None

    def test_merge_point_makes_relative_unknown(self, tmp_path):
        write_migration(tmp_path, "aaaa", None)
        write_migration(tmp_path, "bbbb", "aaaa")
        write_migration(tmp_path, "cccc", "aaaa")
        write_migration(tmp_path, "mmmm", ("bbbb", "cccc"))
        graph = build_revision_graph(tmp_path)
        assert revisions_to_revert(graph, {"mmmm"}, "-1") is None
        # Down to a revision is still well defined across a merge.
        assert set(revisions_to_revert(graph, {"mmmm"}, "aaaa")) == {"mmmm", "bbbb", "cccc"}
        assert revisions_to_revert(graph, {"mmmm"}, "aaaa")[0] == "mmmm"


class TestIrreversibleReason:
    def test_reason_is_read(self, linear):
        assert irreversible_reason(linear, "cccc") == "drops data"

    def test_reversible(self, linear):
        assert irreversible_reason(linear, "dddd") is None

    def test_true_without_reason(self, tmp_path):
        write_migration(tmp_path, "aaaa", None, "irreversible = True")
        assert irreversible_reason(build_revision_graph(tmp_path), "aaaa") == "(no reason given)"

    def test_unknown_revision(self, linear):
        assert irreversible_reason(linear, "zzzz") is None

    def test_marker_is_read_without_executing_module(self, tmp_path):
        write_migration(
            tmp_path,
            "aaaa",
            None,
            'irreversible: str = "lost data"\nraise RuntimeError("must not import")',
        )
        assert irreversible_reason(build_revision_graph(tmp_path), "aaaa") == "lost data"

    def test_false_marker_is_reversible(self, tmp_path):
        write_migration(tmp_path, "aaaa", None, "irreversible = False")
        assert irreversible_reason(build_revision_graph(tmp_path), "aaaa") is None


def test_exception_carries_reason():
    error = IrreversibleMigration("cccc", "drops data")
    assert error.reason == "drops data"
    assert "cccc" in str(error) and "drops data" in str(error)


def write_migration(versions: Path, revision: str, down: DownRevision, extra: str = "") -> None:
    (versions / f"{revision}.py").write_text(
        f'"""{revision}"""\n'
        f"revision = {revision!r}\n"
        f"down_revision = {down!r}\n"
        f"{extra}\n"
    )


@pytest.fixture
def linear(tmp_path):
    """a <- b <- c <- d, with c irreversible."""
    write_migration(tmp_path, "aaaa", None)
    write_migration(tmp_path, "bbbb", "aaaa")
    write_migration(tmp_path, "cccc", "bbbb", 'irreversible = "drops data"')
    write_migration(tmp_path, "dddd", "cccc")
    return build_revision_graph(tmp_path)
