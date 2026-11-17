"""The statements `access apply` sends for a change (design note, 2.5).

The integration tests run them on a server; these pin their text: a replace and the grant
options after it, names quoted, default roles, and ON CLUSTER with its settings.
"""

import pytest
from fake_access_server import FakeServer, grant_rows

from ch_migrate.access.live import LiveState, Wanted, read_live
from ch_migrate.access.model import Grant, Target
from ch_migrate.access.planner import Change, Entity, build_plan
from ch_migrate.access.privileges import bundled_tree
from ch_migrate.access.writes import (
    ON_CLUSTER_SETTINGS,
    Unkept,
    Writing,
    best_effort_writes,
    writes_for,
)

READER = Entity("role", "reader")


def held(*grants, revoked=None):
    """One read of a server where `reader` holds these grants, less a `revoked` one inside."""
    fake = FakeServer(storage="local_directory")
    fake.tables["roles"].append({"name": "reader"})
    rows = [row for grant in grants for row in grant_rows(grant)]
    if revoked is not None:
        rows += grant_rows(revoked, is_partial_revoke=1)
    fake.tables["grants"] += [{"user_name": None, "role_name": "reader", **row} for row in rows]
    return read_live(fake, Wanted(("reader",)))


def replace_writes(spec, live):
    """The SQL of the one replace that the plan for `reader` holds."""
    [change] = [change for change in build_plan(spec, live).changes if change.action == "replace"]
    return [write.sql for write in writes_for(change, Writing(spec, live))]


def sql(change, spec, cluster=None):
    [write] = writes_for(change, Writing(spec, LiveState(bundled_tree()), cluster))
    return write.sql


def test_a_replace_gives_the_whole_set_then_the_grant_options(validate):
    spec = validate(
        """
        roles:
          reader:
            grants: ['SELECT ON {db}.events', 'INSERT ON {db}.events WITH GRANT OPTION']
        """
    ).spec
    live = held(Grant(("SELECT", "INSERT"), Target("shop_dev"), grant_option=True))
    assert replace_writes(spec, live) == [
        "GRANT SELECT ON `shop_dev`.`events`, INSERT ON `shop_dev`.`events` TO `reader` "
        "WITH REPLACE OPTION",
        "GRANT INSERT ON `shop_dev`.`events` TO `reader` WITH GRANT OPTION",
    ]


def test_a_replace_holds_only_what_is_held_of_a_best_effort_grant(validate):
    """The admin may not hold CLUSTER, so a replace never asks for it; a try comes later."""
    spec = validate(
        """
        roles:
          reader:
            grants:
              - SELECT ON {db}.events
              - {grant: SELECT ON system.parts, best_effort: true}
              - {grant: CLUSTER ON *.*, best_effort: true}
        """
    ).spec
    live = held(Grant(("SELECT",), Target("shop_dev")), Grant(("SELECT",), Target("system")))
    assert replace_writes(spec, live) == [
        "GRANT SELECT ON `shop_dev`.`events`, SELECT ON `system`.`parts` TO `reader` "
        "WITH REPLACE OPTION",
    ]


SYSTEM_BEST_EFFORT = """
roles:
  reader:
    grants:
      - SELECT ON {db}.events
      - {grant: SELECT ON system.*, best_effort: true}
"""


def test_a_replace_keeps_what_is_held_inside_a_best_effort_grant(validate):
    """`SELECT ON system.*` declared best-effort, only `system.tables` held."""
    live = held(
        Grant(("SELECT",), Target("shop_dev")),
        Grant(("SELECT",), Target("system", "tables")),
    )
    assert replace_writes(validate(SYSTEM_BEST_EFFORT).spec, live) == [
        "GRANT SELECT ON `shop_dev`.`events`, SELECT ON `system`.`tables` TO `reader` "
        "WITH REPLACE OPTION",
    ]


@pytest.mark.parametrize("above", [Target("system"), Target()], ids=["system.*", "*.*"])
def test_a_revoke_inside_a_best_effort_grant_stops_the_replace(validate, above):
    """`SELECT ON system.*`, or on `*.*`, held but not on `system.users`.
    No GRANT can keep that exactly, so the replace isn't sent, and the role keeps its grants."""
    live = held(
        Grant(("SELECT",), Target("shop_dev")),
        Grant(("SELECT",), above),
        revoked=Grant(("SELECT",), Target("system", "users")),
    )
    with pytest.raises(Unkept) as unkept:
        replace_writes(validate(SYSTEM_BEST_EFFORT).spec, live)
    assert str(unkept.value) == (
        "what it holds of the best-effort grant SELECT ON system.* has SELECT ON system.users "
        "revoked inside it, which a replace can't keep, so it keeps the access it has"
    )


@pytest.mark.parametrize("option", [False, True])
def test_a_revoked_grant_option_inside_stops_a_replace_that_keeps_the_option(validate, option):
    """The grant option of `SELECT ON system.*` held, but not on `system.users`. That stops
    the replace only where the best-effort grant carries the option."""
    declared = "SELECT ON system.* WITH GRANT OPTION" if option else "SELECT ON system.*"
    spec = validate(SYSTEM_BEST_EFFORT.replace("SELECT ON system.*", declared)).spec
    live = held(
        Grant(("SELECT",), Target("shop_dev")),
        Grant(("SELECT",), Target("system"), grant_option=True),
        revoked=Grant(("SELECT",), Target("system", "users"), grant_option=True),
    )
    if option:
        with pytest.raises(Unkept, match="has the grant option for SELECT ON system.users "):
            replace_writes(spec, live)
    else:
        assert replace_writes(spec, live) == [
            "GRANT SELECT ON `shop_dev`.`events`, SELECT ON `system`.* TO `reader` "
            "WITH REPLACE OPTION",
        ]


def test_the_migration_role_is_never_replaced(validate):
    spec = validate("{}\n").spec
    change = Change("replace", Entity("role", "shop_migration_role"))
    with pytest.raises(ValueError, match="only ever added to"):
        writes_for(change, Writing(spec, LiveState(bundled_tree())))


@pytest.mark.parametrize(
    ("grant", "expected"),
    [
        (Grant(("SELECT(we`ird)",), Target("odd-db", "t")), "SELECT(`we\\`ird`) ON `odd-db`.`t`"),
        (Grant(("READ",), Target(source="S3")), "READ ON S3"),
        (Grant(("TABLE ENGINE",), Target(source="MergeTree")), "TABLE ENGINE ON MergeTree"),
        (Grant(("CREATE ROLE",), Target()), "CREATE ROLE ON *.*"),
    ],
)
def test_names_read_from_the_server_are_quoted(validate, grant, expected):
    spec = validate("{}\n").spec
    assert sql(Change("revoke", READER, grant=grant), spec) == f"REVOKE {expected} FROM `reader`"


@pytest.mark.parametrize(
    ("roles", "expected"), [(None, "ALL"), ((), "NONE"), (("a", "b"), "`a`, `b`")]
)
def test_default_roles(validate, roles, expected):
    change = Change("set_default_roles", Entity("user", "u"), roles=roles)
    assert sql(change, validate("{}\n").spec) == f"ALTER USER `u` DEFAULT ROLE {expected}"


def test_on_cluster_statements_wait_for_every_host(validate):
    spec = validate("{}\n").spec
    change = Change("grant_role", Entity("user", "u"), roles=("r",))
    [write] = writes_for(change, Writing(spec, LiveState(bundled_tree()), "it_cluster"))
    assert write.sql == "GRANT ON CLUSTER `it_cluster` `r` TO `u`"
    assert write.settings["wait_end_of_query"] == 1
    drop = "DROP ROLE IF EXISTS `reader` ON CLUSTER `c`"
    assert sql(Change("drop_role", READER), spec, "c") == drop


def test_every_on_cluster_form_quotes_the_cluster(validate):
    """The cluster's name is quoted, so one such as `c-1` parses in all 15 forms."""
    spec = validate("roles: {reader: {grants: ['SELECT ON {db}.events']}}\n").spec
    writing = Writing(spec, LiveState(bundled_tree()), "c-1", {"u": "secret"})
    sql = [write.sql for change in every_form() for write in writes_for(change, writing)]
    assert len(sql) == 15
    assert all(text.count(" ON CLUSTER `c-1`") == 1 for text in sql), sql
    assert "GRANT ON CLUSTER `c-1` `reader` TO `u`" in sql


def test_a_best_effort_grant_is_current_grants_or_a_plain_grant_per_privilege():
    """Decision 12: ClickHouse refuses CURRENT GRANTS ON CLUSTER, so there each privilege is a
    GRANT of its own, which the admin may refuse without stopping the others."""
    names = ("SYSTEM SYNC REPLICA", "SYSTEM SYNC DATABASE REPLICA")
    grant = Grant(names, Target("db"), grant_option=True, best_effort=True)
    [write] = best_effort_writes("r", grant, None)
    assert (write.sql, write.settings, write.gives) == (
        "GRANT CURRENT GRANTS(SYSTEM SYNC REPLICA, SYSTEM SYNC DATABASE REPLICA ON `db`.*) "
        "TO `r` WITH GRANT OPTION",
        {},
        grant,
    )
    writes = best_effort_writes("r", grant, "c")
    assert [write.sql for write in writes] == [
        f"GRANT ON CLUSTER `c` {name} ON `db`.* TO `r` WITH GRANT OPTION" for name in names
    ]
    assert all(write.settings == ON_CLUSTER_SETTINGS for write in writes)
    assert [write.gives.privileges for write in writes] == [(name,) for name in names]


def every_form():
    """One change for each statement in `sql/apply.sql` that can run ON CLUSTER."""
    user, select = Entity("user", "u"), Grant(("SELECT",), Target("db", "t"))
    optioned = Grant(("SELECT",), Target("db", "t"), grant_option=True)
    return [
        Change("create_role", READER),
        Change("create_user", user),
        Change("reset_password", user),
        Change("grant_role", user, roles=("reader",)),
        Change("grant", READER, grant=select),
        Change("grant", READER, grant=optioned),
        Change("replace", READER),
        Change("best_effort", READER, grant=optioned),
        Change("set_default_roles", user, roles=("reader",)),
        Change("revoke", READER, grant=select),
        Change("revoke_grant_option", READER, grant=select),
        Change("revoke_role", user, roles=("reader",)),
        Change("revoke_admin_option", user, roles=("reader",)),
        Change("drop_user", user),
        Change("drop_role", READER),
    ]
