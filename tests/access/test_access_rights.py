"""Rights: grants compared flag by flag, as the spike record's "The comparison" describes.

The row shapes below are what ClickHouse 26.3 stores for each sequence of statements, read
from `system.grants` on Docker. The privilege tree is the bundled 26.3 list.
"""

import pytest

from ch_migrate.access.grants import parse_grant
from ch_migrate.access.privileges import bundled_tree
from ch_migrate.access.rights import (
    Holdings,
    Right,
    Scope,
    StoredRow,
    Surplus,
    fold,
    grant_problems,
    rights_of,
    stored_rows,
)

TREE = bundled_tree()
EVERYWHERE, DB, EVENTS = Scope(), Scope("d"), Scope("d", "events")
REMOTE = Scope(source="REMOTE")


def row(scope, flag="SELECT", **marks):
    """A stored row; `option=True` and `revoke=True` mark it."""
    return StoredRow(flag, scope, marks.get("option", False), marks.get("revoke", False))


def right(scope, option=False, flag="SELECT"):
    return Right(flag, scope, option)


@pytest.mark.parametrize(
    ("outer", "inner", "contained"),
    [
        (EVERYWHERE, EVENTS, True),
        (EVERYWHERE, REMOTE, True),
        (DB, EVENTS, True),
        (DB, DB, True),
        (EVENTS, DB, False),
        (DB, Scope("other", "events"), False),
        (EVENTS, Scope("d", "events", "id"), True),
        (Scope("d", "events", "id"), EVENTS, False),
        (Scope("d", "events", "id"), Scope("d", "events", "name"), False),
        (REMOTE, REMOTE, True),
        (REMOTE, Scope(source="S3"), False),
        (REMOTE, DB, False),
        (DB, REMOTE, False),
        (REMOTE, EVERYWHERE, False),
    ],
)
def test_a_scope_contains_what_is_inside_it(outer, inner, contained):
    assert outer.contains(inner) is contained


def test_a_grant_means_the_flags_its_scope_allows():
    """`CREATE ON db.*` is four flags; on a table `ALTER` loses the `ALTER DATABASE` flags."""
    create = rights_of(TREE, parse_grant("CREATE ON d.*"))
    assert {r.flag for r in create} == {
        "CREATE DATABASE",
        "CREATE TABLE",
        "CREATE VIEW",
        "CREATE DICTIONARY",
    }
    on_table = {r.flag for r in rights_of(TREE, parse_grant("ALTER ON d.events"))}
    on_database = {r.flag for r in rights_of(TREE, parse_grant("ALTER ON d.*"))}
    assert on_database - on_table == TREE.flags("ALTER DATABASE", "database") != frozenset()
    assert rights_of(TREE, parse_grant("DELETE ON d.*")) == {right(DB, flag="ALTER DELETE")}
    assert rights_of(TREE, parse_grant("READ ON REMOTE")) == {right(REMOTE, flag="READ")}
    assert rights_of(TREE, parse_grant("SELEKT ON d.*")) == frozenset()


def test_what_the_server_would_reject_is_named():
    assert grant_problems(TREE, parse_grant("SELECT, SELEKT ON d.*")) == [
        "the server has no privilege named SELEKT"
    ]
    assert grant_problems(TREE, parse_grant("CREATE USER ON d.*")) == [
        "the server can't grant CREATE USER on a database"
    ]
    assert grant_problems(TREE, parse_grant("SELECT ON d.events")) == []


def test_a_broader_grant_holds_a_narrower_right_and_an_option_holds_the_plain_right():
    """Folding cancels out: no row is stored for a grant that a broader one absorbs."""
    held = Holdings((row(DB, option=True),))
    assert held.holds(right(EVENTS)) and held.holds(right(EVENTS, option=True))
    assert not held.holds(right(EVERYWHERE)) and not held.holds(right(Scope("other")))
    plain = Holdings((row(DB),))
    assert plain.holds(right(DB)) and not plain.holds(right(DB, option=True))


def test_a_partial_revoke_takes_the_right_away_inside_a_broader_grant():
    """`GRANT SELECT ON d.* WITH GRANT OPTION`, then `REVOKE SELECT ON d.events`."""
    held = Holdings((row(DB, option=True), row(EVENTS, revoke=True)))
    assert not held.holds(right(EVENTS))
    assert not held.holds(right(DB)), "a hole inside it: the declared grant isn't whole"
    assert held.holds(right(Scope("d", "other"), option=True))


def test_revoking_only_the_grant_option_leaves_the_right():
    """`GRANT SELECT ON d.* WITH GRANT OPTION`, then `REVOKE GRANT OPTION FOR ... ON d.events`."""
    held = Holdings((row(DB, option=True), row(EVENTS, option=True, revoke=True)))
    assert held.holds(right(EVENTS)) and held.holds(right(DB))
    assert not held.holds(right(EVENTS, option=True))
    assert not held.holds(right(DB, option=True))


def test_the_nearest_row_decides():
    """`GRANT ON *.*`, `REVOKE ON d.*`, `GRANT ON d.events`: the table grant wins there."""
    held = Holdings((row(EVERYWHERE), row(DB, revoke=True), row(EVENTS)))
    assert held.holds(right(EVENTS))
    assert not held.holds(right(Scope("d", "other"))) and not held.holds(right(DB))
    assert held.holds(right(Scope("elsewhere")))
    assert not held.holds(right(EVERYWHERE))


def test_an_option_on_a_table_inside_a_plain_database_grant():
    """`GRANT SELECT ON d.*`, then `GRANT SELECT ON d.events WITH GRANT OPTION`."""
    held = Holdings((row(DB), row(EVENTS, option=True)))
    assert held.holds(right(EVENTS, option=True)) and held.holds(right(DB))
    assert not held.holds(right(DB, option=True))


def test_surplus_is_what_no_desired_right_covers():
    held = Holdings((row(DB, option=True), row(DB, flag="INSERT"), row(EVENTS, revoke=True)))
    assert held.surplus(frozenset({right(DB, option=True), right(DB, flag="INSERT")})) == ()
    assert held.surplus(frozenset({right(DB)})) == (
        Surplus(row(DB, option=True), option_only=True),
        Surplus(row(DB, flag="INSERT")),
    )
    assert held.surplus(frozenset({right(EVENTS)})) == (
        Surplus(row(DB, option=True)),
        Surplus(row(DB, flag="INSERT")),
    )


@pytest.mark.parametrize(
    ("surplus", "desired", "undermines"),
    [
        # Other flags on the same scope: a plain revoke.
        (Surplus(row(DB, flag="INSERT")), right(DB), False),
        # The same flag and scope, option only: REVOKE GRANT OPTION FOR.
        (Surplus(row(DB, option=True), option_only=True), right(DB), False),
        # The same flag on a strictly broader scope: revoking it takes the declared right.
        (Surplus(row(DB)), right(EVENTS), True),
        (Surplus(row(EVERYWHERE)), right(DB), True),
        (Surplus(row(EVERYWHERE, flag="READ")), right(REMOTE, flag="READ"), True),
        # Only the option goes: a plain right inside survives, one with option doesn't.
        (Surplus(row(DB, option=True), option_only=True), right(EVENTS), False),
        (Surplus(row(DB, option=True), option_only=True), right(EVENTS, option=True), True),
        (Surplus(row(DB)), right(Scope("other", "events")), False),
    ],
)
def test_a_surplus_undermines_a_desired_right_strictly_inside_it(surplus, desired, undermines):
    assert surplus.undermines([desired]) is undermines


def test_fold_names_a_group_only_when_every_flag_of_it_is_there():
    """Narrowing ALTER to ALTER UPDATE revokes the other leaves, never ALTER itself."""
    alter = TREE.flags("ALTER", "database")
    assert fold(TREE, alter, "database") == ("ALTER",)
    others = fold(TREE, alter - {"ALTER UPDATE"}, "database")
    assert "ALTER" not in others and "ALTER UPDATE" not in others and "ALTER DELETE" in others
    assert len(others) < len(alter) - 1
    unfolded = frozenset().union(*(TREE.flags(name, "database") for name in others))
    assert unfolded == alter - {"ALTER UPDATE"}
    assert fold(TREE, TREE.flags("ALL", "database"), "database") == ("ALL",)
    assert fold(TREE, {"READ", "WRITE"}, "source") == ("READ", "WRITE"), "never ALL ON S3"
    assert fold(TREE, {"SELECT", "INSERT", "NOT A FLAG"}, "table") == (
        "INSERT",
        "NOT A FLAG",
        "SELECT",
    )


def test_a_stored_row_becomes_its_flags_on_its_scope():
    stored = {
        "access_type": "READ",
        "access_object": "REMOTE",
        "database": None,
        "table": None,
        "column": None,
        "is_partial_revoke": 0,
        "grant_option": 1,
    }
    assert stored_rows(TREE, stored) == [StoredRow("READ", REMOTE, True, False)]
    every_source = {**stored, "access_object": "", "grant_option": 0}
    assert stored_rows(TREE, every_source) == [StoredRow("READ", EVERYWHERE)]
    column = {**every_source, "access_type": "SELECT", "database": "d", "table": "events"}
    assert stored_rows(TREE, {**column, "column": "id", "is_partial_revoke": 1}) == [
        StoredRow("SELECT", Scope("d", "events", "id"), False, True)
    ]
    group = stored_rows(TREE, {**column, "access_type": "ALTER TABLE", "table": None})
    assert {flag_row.flag for flag_row in group} == TREE.flags("ALTER TABLE", "database")


def test_a_row_the_tree_cannot_expand_is_kept_under_its_own_name():
    """So it still counts as held, and is revoked or listed like any other row."""
    stored = {
        "access_type": "SOMETHING NEW",
        "access_object": "",
        "database": "d",
        "table": None,
        "column": None,
        "is_partial_revoke": 0,
        "grant_option": 0,
    }
    assert stored_rows(TREE, stored) == [StoredRow("SOMETHING NEW", DB)]
