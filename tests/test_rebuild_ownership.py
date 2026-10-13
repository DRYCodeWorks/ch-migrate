"""Cloud rebuild ownership: an owner token proves an object is ours; nothing else is adopted."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ch_migrate.rebuild_engine import _Context
from ch_migrate.waiting_types import UnknownOutcome

HOSTS = {"replica-a", "replica-b"}
TOKEN = "chm-rebuild:r1:snap:abc"


def test_unbound_helper_with_our_token_everywhere_binds_its_server_uuid():
    ctx = _cloud_context({host: ("u1", "MergeTree", TOKEN) for host in HOSTS})
    assert ctx.proven("snap")
    assert ctx.bound_uuid("snap") == "u1"
    assert ctx.state["helpers"]["snap"] == "u1"
    assert ctx.saved == 1


def test_absent_or_partly_visible_helper_is_not_proven_and_cannot_bind():
    ctx = _cloud_context({"replica-a": ("u1", "MergeTree", TOKEN)})
    assert not ctx.proven("snap")
    with pytest.raises(UnknownOutcome):
        ctx.bound_uuid("snap")


def test_another_owners_comment_is_never_adopted():
    catalog = {"replica-a": ("u1", "MergeTree", TOKEN), "replica-b": ("u1", "MergeTree", "x")}
    ctx = _cloud_context(catalog)
    with pytest.raises(UnknownOutcome, match="another owner"):
        ctx.proven("snap")


def test_replicas_disagreeing_on_uuid_cannot_bind():
    catalog = {"replica-a": ("u1", "MergeTree", TOKEN), "replica-b": ("u2", "MergeTree", TOKEN)}
    ctx = _cloud_context(catalog)
    assert not ctx.proven("snap")
    with pytest.raises(UnknownOutcome):
        ctx.bound_uuid("snap")


def test_bound_helper_whose_comment_was_edited_fails_closed():
    ctx = _cloud_context({host: ("u1", "MergeTree", "edited") for host in HOSTS})
    ctx.state["helpers"]["snap"] = "u1"
    with pytest.raises(UnknownOutcome, match="another owner"):
        ctx.proven("snap")


def test_bound_new_is_proven_by_uuid_after_its_comment_is_restored():
    ctx = _cloud_context({host: ("u9", "ReplacingMergeTree", "live events") for host in HOSTS})
    ctx.state["helpers"]["new"] = "u9"
    assert ctx.proven("new")
    ctx.state["helpers"]["new"] = "other"
    assert not ctx.proven("new")


def _cloud_context(catalog):
    ctx = object.__new__(_Context)
    ctx.shared = True
    ctx.hosts = HOSTS
    ctx.table = "events"
    ctx.saved = 0
    record = {
        "rebuild": {
            "helpers": {"new": None, "snap": None, "stage": None},
            "tokens": {role: f"chm-rebuild:r1:{role}:abc" for role in ("new", "snap", "stage")},
            "dual_uuid": None,
        }
    }

    def checkpoint():
        ctx.saved += 1

    ctx.runtime = SimpleNamespace(record=record, checkpoint=checkpoint)
    ctx.catalog = lambda name: dict(catalog)
    return ctx
