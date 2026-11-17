"""The access spike: how ClickHouse stores and checks grants (U1–U10 in the design note).

Each test pins one answer recorded in `docs/design/2026-11-11-access-spike.md`. A test about a
feature that some images lack asserts what those images do instead; it never skips.
`CH_MIGRATE_IT_IMAGE` picks the image. The u5 and u8 tests hold expectations for 24.8, 25.8
and 26.3, and fail on any other image until someone records what that image does.

The core profile below is the design note's draft (2.4), written out by hand: there is no
product code for it yet. Users and roles a test creates start with the project's `it_<hex>`
name, so the access fixtures' finalizer drops them. Cluster tests drop theirs on every node.
"""

import hashlib
import secrets
import statistics
import threading
import time
from dataclasses import dataclass, replace

import pytest
from access_fixtures import AccessProject, initialize_cluster_project, query_log
from clickhouse_connect.driver.exceptions import DatabaseError, OperationalError
from grant_rights import (
    PrivilegeTree,
    Right,
    Scope,
    admin_rights,
    compare,
    desired_rights,
    gives_in_full,
    held_in_full,
    live_rights,
    parse_grant,
)

from ch_migrate.bootstrap import build_bootstrap_sql

pytestmark = pytest.mark.integration

# The design note's draft core profile (2.4), with `{db}` for the project database.
CORE_REQUIRED = (
    "CREATE TABLE, DROP TABLE, UNDROP TABLE, ALTER ON {db}.*",
    "CREATE VIEW, DROP VIEW, CREATE DICTIONARY, DROP DICTIONARY ON {db}.*",
    "SELECT, INSERT, DELETE, TRUNCATE, OPTIMIZE ON {db}.*",
    "SHOW TABLES, SHOW COLUMNS, SHOW DICTIONARIES ON {db}.*",
    "SELECT ON system.databases",
    "SELECT ON system.tables",
    "SELECT ON system.mutations",
    "SELECT ON system.dictionaries",
)
CORE_BEST_EFFORT = (
    "SELECT ON system.parts",
    "SELECT ON system.replicas",
    "SYSTEM SYNC REPLICA, SYSTEM SYNC DATABASE REPLICA ON {db}.*",
    "CLUSTER ON *.*",
    "READ ON REMOTE",
    "CREATE TEMPORARY TABLE ON *.*",
)
# U6: the smallest admin that runs a core-only bootstrap. Every entry is needed. The admin
# needs no SHOW grants: ClickHouse implies SHOW TABLES, SHOW COLUMNS and SHOW DICTIONARIES,
# with grant option, from its other rights on the database.
SMALLEST_ADMIN = (
    "CREATE DATABASE ON {db}.*",
    "CREATE ROLE ON *.*",
    "CREATE USER ON *.*",
    "ROLE ADMIN ON *.*",
    *(grant + " WITH GRANT OPTION" for grant in CORE_REQUIRED if not grant.startswith("SHOW")),
)
# U1: grants ClickHouse stores in another shape, and the rows it writes for them.
# Each row is (access_type, database, table, access_object).
STORED_SHAPES = {
    "a narrower privilege folds into its group": (
        ("ALTER, DELETE ON {db}.*",),
        {("ALTER", "{db}", None, "")},
    ),
    "an alias is stored under its canonical name": (
        ("DELETE ON {db}.*",),
        {("ALTER DELETE", "{db}", None, "")},
    ),
    "a parent is stored as the children its scope allows": (
        ("CREATE ON {db}.*",),
        {(name, "{db}", None, "") for name in ("CREATE DATABASE", "CREATE TABLE")}
        | {(name, "{db}", None, "") for name in ("CREATE VIEW", "CREATE DICTIONARY")},
    ),
    "a database-scoped ACCESS MANAGEMENT keeps only its row-policy children": (
        ("ACCESS MANAGEMENT ON {db}.*",),
        {
            (name, "{db}", None, "")
            for name in (
                "CREATE ROW POLICY",
                "ALTER ROW POLICY",
                "DROP ROW POLICY",
                "SHOW ROW POLICIES",
            )
        },
    ),
    "a table-scoped ALTER drops ALTER DATABASE": (
        ("ALTER ON {db}.events",),
        {("ALTER TABLE", "{db}", "events", ""), ("ALTER VIEW", "{db}", "events", "")},
    ),
    # On a database the admin holds all of: ClickHouse Cloud's admin can't give SELECT on all
    # of `system.*`, which takes in `system.zookeeper`.
    "a narrower grant is absorbed by a broader one": (
        ("SELECT ON {db}.events", "SELECT ON {db}.*"),
        {("SELECT", "{db}", None, "")},
    ),
    "a grant without option is absorbed by the same grant with it": (
        ("SELECT ON {db}.* WITH GRANT OPTION", "SELECT ON {db}.events"),
        {("SELECT", "{db}", None, "")},
    ),
    "READ ON REMOTE is a source grant": (
        ("READ ON REMOTE",),
        {("READ", None, None, "REMOTE")},
    ),
}
# Expectations per image (major.minor). An image with no entry fails the u5 and u8 tests.
IMAGES = {
    "24.8": {"check_grant": False, "source_grants": False, "create_user_level": "GLOBAL"},
    "25.8": {"check_grant": True, "source_grants": True, "create_user_level": "USER_NAME"},
    "26.3": {"check_grant": True, "source_grants": True, "create_user_level": "USER_NAME"},
}
# How SHOW GRANTS prints `READ ON REMOTE` on each image, as it starts, with
# `access_control_improvements.enable_read_write_grants` off. ClickHouse Cloud turns it on,
# and prints the grant as written.
SHOWN_READ_ON_REMOTE = {"25.8": "GRANT REMOTE ON *.*", "26.3": "GRANT REMOTE ON *.*"}
# U7: a migration that uses each core database right ch-migrate's own paths don't. The
# dictionary reads its source as the migration user, as `create_dictionary` names a user:
# ClickHouse Cloud refuses a ClickHouse source with no USER from any login but `default`.
U7_UPGRADE = """
CREATE TABLE IF NOT EXISTS {db}.events (id UInt64, status String) ENGINE = MergeTree ORDER BY id;
-- ch-migrate: allow-non-idempotent The spike's fixture table starts empty
INSERT INTO {db}.events VALUES (1, 'new'), (2, 'old');
ALTER TABLE {db}.events ADD COLUMN IF NOT EXISTS note String DEFAULT '';
-- ch-migrate: allow-non-idempotent A second run writes the same value
ALTER TABLE {db}.events UPDATE status = 'seen' WHERE id = 1;
-- ch-migrate: allow-non-idempotent Deleting an absent row again is harmless
DELETE FROM {db}.events WHERE id = 2;
CREATE VIEW IF NOT EXISTS {db}.recent AS SELECT id, status FROM {db}.events;
CREATE DICTIONARY IF NOT EXISTS {db}.statuses (id UInt64, status String) PRIMARY KEY id
SOURCE(CLICKHOUSE(DB '{db}' TABLE 'events' USER '<user>' PASSWORD '<password>'))
LIFETIME(0) LAYOUT(FLAT());
OPTIMIZE TABLE {db}.events FINAL;
"""
U7_DOWNGRADE = """
DROP DICTIONARY IF EXISTS {db}.statuses;
DROP VIEW IF EXISTS {db}.recent;
TRUNCATE TABLE IF EXISTS {db}.events;
DROP TABLE IF EXISTS {db}.events;
"""
U7_CLUSTER_UPGRADE = """
CREATE TABLE IF NOT EXISTS {db}.events ON CLUSTER it_cluster (id UInt64, status String)
ENGINE = ReplicatedMergeTree('/clickhouse/tables/{db}/events', '{replica}') ORDER BY id;
-- ch-migrate: allow-non-idempotent A second run writes the same value
ALTER TABLE {db}.events ON CLUSTER it_cluster UPDATE status = 'seen' WHERE id = 1;
"""
U7_CLUSTER_DOWNGRADE = "DROP TABLE IF EXISTS {db}.events ON CLUSTER it_cluster SYNC;"
U7_HELD_UPGRADE = """
-- ch-migrate: allow-non-idempotent A second run writes the same value
ALTER TABLE {db}.events ON CLUSTER it_cluster UPDATE status = 'seen' WHERE id = 1;
"""


@dataclass(frozen=True)
class Identity:
    """A role, and a user holding it, made for one test."""

    database: str
    role: str
    user: str
    password: str


# U1. Read-only comparison: leaf expansion from system.privileges.


def test_u1_every_privilege_on_every_target(access_project):
    """Grant each privilege on `*.*`, `db.*` and `db.t`, and compare by leaf expansion.

    Every accepted grant matches, except the old source privileges (`REMOTE ON *.*` and
    the like), which servers with `READ ON <source>` store as READ and WRITE on that source.
    The server rejects a grant only when the expansion is empty, or when the admin's own
    rights don't cover it with grant option. More grants are accepted than there are
    privileges: a stock server's admin can give them all, and Cloud's most.
    """
    client, tree = access_project.client, PrivilegeTree.read(access_project.client)
    _create_events_table(access_project)
    held_by_admin = admin_rights(client, tree)
    role = access_project.name + "_sweep"
    accepted = 0
    for privilege in sorted(tree.levels):
        for target in ("*.*", "{db}.*", "{db}.events"):
            grant = parse_grant(f"{privilege} ON {target.format(db=access_project.database)}")
            desired = desired_rights(tree, [grant])
            error = _grant_to_fresh_role(client, grant, role)
            if error:
                _assert_rejection_explained(error, desired, held_by_admin)
                continue
            accepted += 1
            difference = compare(desired, live_rights(tree, _grant_rows(client, "role", role)))
            if not difference.in_sync:
                _assert_source_rewrite(grant, difference)
    assert accepted > len(tree.levels)


def test_u1_profiles_match_one_grant_at_a_time_and_together(access_project):
    """Both profiles: each statement alone, then the whole profile on one role."""
    client = access_project.client
    role = access_project.name + "_migration_role"
    for profile in (_legacy_grants(access_project), _core_grants(access_project)):
        profile = _given_in_full(client, profile)
        for statement in profile:
            _reset_role(client, role)
            client.command(_as_written(statement, role))
            _assert_in_sync(client, role, [statement])
        _reset_role(client, role)
        for statement in profile:
            client.command(_as_written(statement, role))
        _assert_in_sync(client, role, profile)


@pytest.mark.parametrize("case", sorted(STORED_SHAPES))
def test_u1_folded_grants_still_match(access_project, case):
    """ClickHouse stores these in another shape; leaf expansion still finds them equal."""
    statements, stored = STORED_SHAPES[case]
    client = access_project.client
    role = access_project.name + "_folded"
    db = access_project.database
    statements = [statement.format(db=db) for statement in statements]
    _reset_role(client, role)
    for statement in statements:
        client.command(_grant_sql(parse_grant(statement), role))
    rows = _grant_rows(client, "role", role)
    expected = {(t, d if d != "{db}" else db, table, o) for t, d, table, o in stored}
    assert {_row_key(row) for row in rows} == expected
    _assert_in_sync(client, role, statements)


def test_u1_show_grants_text_differs_from_what_is_stored(access_project):
    """Why a text diff of SHOW GRANTS fails: READ ON REMOTE reads back as REMOTE ON *.*."""
    client = access_project.client
    shown = SHOWN_READ_ON_REMOTE.get(_image_version(client))
    if shown is None:
        pytest.skip(
            "No recorded SHOW GRANTS text for this image; record it in SHOWN_READ_ON_REMOTE"
        )
    role = access_project.name + "_text"
    _reset_role(client, role)
    client.command(f"GRANT READ ON REMOTE TO {role}")
    assert client.command(f"SHOW GRANTS FOR {role}") == f"{shown} TO {role}"
    assert [_row_key(row) for row in _grant_rows(client, "role", role)] == [
        ("READ", None, None, "REMOTE")
    ]


def test_u1_narrowing_revokes_only_the_difference(access_project):
    """From ALTER to ALTER UPDATE: revoke the extra leaves; never "grant new, revoke old"."""
    client = access_project.client
    db, role = access_project.database, access_project.name + "_narrow"
    _reset_role(client, role)
    client.command(f"GRANT ALTER ON {db}.* TO {role}")
    desired = [f"ALTER UPDATE ON {db}.*"]
    difference = _difference(client, role, desired)
    assert not difference.missing and len(difference.extra) > 10
    flags = ", ".join(sorted(right.privilege for right in difference.extra))
    client.command(f"REVOKE {flags} ON {db}.* FROM {role}")
    _assert_in_sync(client, role, desired)
    assert [_row_key(row) for row in _grant_rows(client, "role", role)] == [
        ("ALTER UPDATE", db, None, "")
    ]
    _reset_role(client, role)
    client.command(f"GRANT ALTER ON {db}.* TO {role}")
    client.command(f"GRANT ALTER UPDATE ON {db}.* TO {role}")
    client.command(f"REVOKE ALTER ON {db}.* FROM {role}")
    assert _grant_rows(client, "role", role) == []


def test_u1_grant_option_is_part_of_each_right(access_project):
    """A grant option the YAML doesn't ask for is extra; one it asks for and lacks is missing."""
    client = access_project.client
    db, role = access_project.database, access_project.name + "_option"
    _reset_role(client, role)
    client.command(f"GRANT SELECT ON {db}.* TO {role} WITH GRANT OPTION")
    plain = _difference(client, role, [f"SELECT ON {db}.*"])
    assert not plain.missing
    assert plain.extra == {Right("SELECT", Scope(db), True)}
    client.command(f"REVOKE GRANT OPTION FOR SELECT ON {db}.* FROM {role}")
    _assert_in_sync(client, role, [f"SELECT ON {db}.*"])
    with_option = _difference(client, role, [f"SELECT ON {db}.* WITH GRANT OPTION"])
    assert with_option.missing == {Right("SELECT", Scope(db), True)}


def test_u1_narrowing_a_scope_cannot_grant_first(access_project):
    """From `db.*` to `db.events`: the narrower grant is absorbed, so the revoke takes it too.

    Revoking first converges, with a moment of no access in between. One
    `GRANT ... WITH REPLACE OPTION` converges in a single statement and keeps memberships.
    """
    client = access_project.client
    db, role = access_project.database, access_project.name + "_scope"
    desired = [f"SELECT ON {db}.events"]
    _reset_role(client, role)
    client.command(f"GRANT SELECT ON {db}.* TO {role}")
    difference = _difference(client, role, desired)
    assert not difference.missing
    assert difference.extra == {Right("SELECT", Scope(db), False)}
    client.command(f"GRANT SELECT ON {db}.events TO {role}")
    client.command(f"REVOKE SELECT ON {db}.* FROM {role}")
    assert _grant_rows(client, "role", role) == []
    client.command(f"GRANT SELECT ON {db}.* TO {role}")
    client.command(f"REVOKE SELECT ON {db}.* FROM {role}")
    client.command(f"GRANT SELECT ON {db}.events TO {role}")
    _assert_in_sync(client, role, desired)
    _assert_replace_converges(access_project, role, desired)


def test_u1_replace_needs_every_current_right_with_grant_option(
    access_project, clickhouse_server, request
):
    """An admin that can't pass on one right the role holds can't replace; nothing changes."""
    client, db = access_project.client, access_project.database
    role = access_project.name + "_replaced"
    _reset_role(client, role)
    client.command(f"GRANT SELECT ON {db}.* TO {role}")
    client.command(f"GRANT SELECT ON {db}_elsewhere.* TO {role}")
    grants = (f"CURRENT GRANTS(ALL ON {db}.*) WITH GRANT OPTION",)  # as much as the admin has
    admin = _admin_login(access_project, clickhouse_server, grants).connect()
    request.addfinalizer(admin.close)
    before = _flagged_rows(client, role)
    with pytest.raises(DatabaseError, match="ACCESS_DENIED"):
        admin.command(f"GRANT SELECT ON {db}.events TO {role} WITH REPLACE OPTION")
    assert _flagged_rows(client, role) == before


def test_u1_column_grant_is_narrower_than_a_table_grant(access_project):
    """A live column grant doesn't satisfy a table grant; the table grant then absorbs it."""
    client = access_project.client
    db, role = access_project.database, access_project.name + "_column"
    _create_events_table(access_project)
    _reset_role(client, role)
    client.command(f"GRANT SELECT(id) ON {db}.events TO {role}")
    difference = _difference(client, role, [f"SELECT ON {db}.events"])
    assert difference.missing == {Right("SELECT", Scope(db, "events"), False)}
    assert not difference.extra
    client.command(f"GRANT SELECT ON {db}.events TO {role}")
    assert [_row_key(row) for row in _grant_rows(client, "role", role)] == [
        ("SELECT", db, "events", "")
    ]


# U2. Partial revokes.


def test_u2_partial_revoke_is_a_row_and_granting_again_clears_it(access_project):
    client = access_project.client
    db, role = access_project.database, access_project.name + "_partial"
    _reset_role(client, role)
    client.command(f"GRANT SELECT ON {db}.* TO {role}")
    client.command(f"REVOKE SELECT ON {db}.events FROM {role}")
    assert _flagged_rows(client, role) == {
        ("SELECT", db, None, 0, 0),
        ("SELECT", db, "events", 0, 1),
    }
    difference = _difference(client, role, [f"SELECT ON {db}.*"])
    assert not difference.missing and not difference.extra
    assert difference.partial_revokes == {Right("SELECT", Scope(db, "events"), False)}
    client.command(f"GRANT SELECT ON {db}.* TO {role}")
    assert _flagged_rows(client, role) == {("SELECT", db, None, 0, 0)}
    _assert_in_sync(client, role, [f"SELECT ON {db}.*"])


def test_u2_revoking_a_child_at_the_same_scope_leaves_no_partial_revoke(access_project):
    client = access_project.client
    db, role = access_project.database, access_project.name + "_child"
    _reset_role(client, role)
    client.command(f"GRANT ALTER ON {db}.* TO {role}")
    client.command(f"REVOKE ALTER DELETE ON {db}.* FROM {role}")
    rows = _flagged_rows(client, role)
    assert all(partial == 0 for *_, partial in rows)
    assert {name for name, *_ in rows}.isdisjoint({"ALTER", "ALTER TABLE", "ALTER DELETE"})
    assert ("ALTER UPDATE", db, None, 0, 0) in rows


def test_u2_revoking_only_the_grant_option_is_a_partial_revoke_row(access_project):
    client = access_project.client
    db, role = access_project.database, access_project.name + "_option_revoke"
    _reset_role(client, role)
    client.command(f"GRANT SELECT ON {db}.* TO {role} WITH GRANT OPTION")
    client.command(f"REVOKE GRANT OPTION FOR SELECT ON {db}.events FROM {role}")
    assert _flagged_rows(client, role) == {
        ("SELECT", db, None, 1, 0),
        ("SELECT", db, "events", 1, 1),
    }
    client.command(f"GRANT SELECT ON {db}.* TO {role} WITH GRANT OPTION")
    assert _flagged_rows(client, role) == {("SELECT", db, None, 1, 0)}


# U3. Grants on objects that don't exist yet.


def test_u3_grants_on_missing_objects_are_kept_and_work_later(
    access_project, clickhouse_server, request
):
    client = access_project.client
    later = access_project.name + "_later_db"
    request.addfinalizer(lambda: client.command(f"DROP DATABASE IF EXISTS {later} SYNC"))
    identity = _identity(access_project, "u3")
    _create_user(client, identity)
    client.command(f"GRANT CREATE TABLE, SELECT, INSERT ON {later}.* TO {identity.user}")
    client.command(f"GRANT SELECT ON {access_project.database}.missing TO {identity.user}")
    assert {_row_key(row)[:3] for row in _grant_rows(client, "user", identity.user)} == {
        ("CREATE TABLE", later, None),
        ("SELECT", later, None),
        ("INSERT", later, None),
        ("SELECT", access_project.database, "missing"),
    }
    client.command(f"CREATE DATABASE {later}")
    user = _connect(clickhouse_server, identity, request)
    user.command(f"CREATE TABLE {later}.t (id UInt64) ENGINE = MergeTree ORDER BY id")
    user.command(f"INSERT INTO {later}.t VALUES (1)")
    assert user.command(f"SELECT count() FROM {later}.t") == 1
    client.command(f"DROP DATABASE {later} SYNC")
    assert len(_grant_rows(client, "user", identity.user)) == 4


# U4. Passwords.


@pytest.mark.parametrize(
    "method",
    ["", "sha256_password", "double_sha1_password", "bcrypt_password"],
    ids=["default", "sha256", "double_sha1", "bcrypt"],
)
def test_u4_nothing_the_admin_reads_shows_a_password_or_hash(access_project, method):
    """The password goes in as a literal, as bootstrap sends it, so the query log could hold it."""
    client = access_project.client
    identity = _identity(access_project, "u4")
    with_method = f" WITH {method}" if method else ""
    client.command(f"CREATE USER {identity.user} IDENTIFIED{with_method} BY '{identity.password}'")
    texts = _admin_views(client, identity.user)
    secrets_ = _password_forms(identity.password)
    for text in texts:
        assert not any(secret in text.lower() for secret in secrets_), text
    assert "$2" not in "".join(texts)  # no bcrypt hash


# U5. CHECK GRANT.


def test_u5_check_grant_on_this_image(access_project, clickhouse_server, request):
    """Supported images answer 1 or 0, and 0 for a partly held group; 24.8 has no CHECK GRANT."""
    client, db = access_project.client, access_project.database
    identity = _identity(access_project, "u5")
    _create_user(client, identity)
    for grant in ("SELECT, ALTER UPDATE ON {db}.*", "SELECT ON {db}_missing.*"):
        client.command(f"GRANT {grant.format(db=db)} TO {identity.user}")
    user = _connect(clickhouse_server, identity, request)
    if not _image(client)["check_grant"]:
        with pytest.raises(DatabaseError, match="SYNTAX_ERROR"):
            user.command(f"CHECK GRANT SELECT ON {db}.*")
        _assert_shows_own_grants(client, user, identity)
        return
    answers = {
        f"SELECT ON {db}.events": 1,
        f"ALTER UPDATE ON {db}.*": 1,
        f"ALTER ON {db}.*": 0,
        f"INSERT ON {db}.*": 0,
        f"SELECT ON {db}_missing.events": 1,
    }
    assert {grant: user.command(f"CHECK GRANT {grant}") for grant in answers} == answers
    with pytest.raises(DatabaseError, match="SYNTAX_ERROR"):
        user.command(f"CHECK GRANT SELECT ON {db}.* WITH GRANT OPTION")


def _assert_shows_own_grants(client, user, identity):
    """Without CHECK GRANT, a user can still read its own grants and its roles'."""
    db = identity.database
    shown = [line for (line,) in user.query("SHOW GRANTS").result_rows]
    assert f"GRANT SELECT, ALTER UPDATE ON {db}.* TO {identity.user}" in shown
    client.command(f"CREATE ROLE {identity.role}")
    client.command(f"GRANT INSERT ON {db}.* TO {identity.role}")
    client.command(f"GRANT {identity.role} TO {identity.user}")
    shown = [line for (line,) in user.query(f"SHOW GRANTS FOR {identity.role}").result_rows]
    assert shown == [f"GRANT INSERT ON {db}.* TO {identity.role}"]
    with pytest.raises(DatabaseError, match="ACCESS_DENIED"):
        user.command("SELECT count() FROM system.grants")


# U6. The smallest admin.


def test_u6_smallest_admin_runs_a_core_bootstrap(access_project, clickhouse_server, request):
    """Every required grant lands; no best-effort grant does, and none raises."""
    client, tree = access_project.client, PrivilegeTree.read(access_project.client)
    admin = _admin_login(access_project, clickhouse_server, SMALLEST_ADMIN).connect()
    request.addfinalizer(admin.close)
    identity = _identity(access_project, "core")
    _core_bootstrap(admin, identity)
    difference = _difference(client, identity.role, _core_grants(access_project))
    best_effort = desired_rights(
        tree, [parse_grant(g) for g in _render(access_project, CORE_BEST_EFFORT)]
    )
    assert difference.missing == best_effort
    assert not difference.extra
    assert client.command(f"EXISTS DATABASE {identity.database}") == 1


@pytest.mark.parametrize("left_out", range(len(SMALLEST_ADMIN)))
def test_u6_each_smallest_admin_grant_is_needed(
    access_project, clickhouse_server, request, left_out
):
    grants = SMALLEST_ADMIN[:left_out] + SMALLEST_ADMIN[left_out + 1 :]
    admin = _admin_login(access_project, clickhouse_server, grants).connect()
    request.addfinalizer(admin.close)
    with pytest.raises(DatabaseError, match="ACCESS_DENIED"):
        _core_bootstrap(admin, _identity(access_project, "core"))


def test_u6_create_database_if_not_exists_needs_the_grant_when_it_exists(
    access_project, clickhouse_server, request
):
    """So a bootstrap against an existing database still needs CREATE DATABASE."""
    access_project.client.command(f"CREATE DATABASE {access_project.database}")
    grants = tuple(g for g in SMALLEST_ADMIN if not g.startswith("CREATE DATABASE"))
    admin = _admin_login(access_project, clickhouse_server, grants).connect()
    request.addfinalizer(admin.close)
    with pytest.raises(DatabaseError, match="ACCESS_DENIED"):
        admin.command(f"CREATE DATABASE IF NOT EXISTS {access_project.database}")


def test_u6_the_t1_limited_admin_cannot_give_three_best_effort_grants(
    access_project, limited_admin, request
):
    client, tree = access_project.client, PrivilegeTree.read(access_project.client)
    admin = limited_admin.connect()
    request.addfinalizer(admin.close)
    identity = _identity(access_project, "core")
    _core_bootstrap(admin, identity)
    difference = _difference(client, identity.role, _core_grants(access_project))
    unreachable = ("CLUSTER ON *.*", "READ ON REMOTE", "CREATE TEMPORARY TABLE ON *.*")
    assert difference.missing == desired_rights(tree, [parse_grant(g) for g in unreachable])
    assert not difference.extra


# U7. What ch-migrate's own paths need.


def test_u7_core_required_alone_runs_these_paths(access_project, monkeypatch):
    """new, up (with a mutation to wait on), status, history, lint, snapshot, diff,
    new --table and down. rebase and deps are not run."""
    identity = _core_migration_user(access_project, CORE_REQUIRED, monkeypatch)
    _write_migration(access_project, _u7_upgrade(identity), U7_DOWNGRADE)
    for command in (["up", "it"], ["status", "it"], ["history", "it"], ["lint", "it"]):
        _assert_runs(access_project, *command)
    _assert_runs(access_project, "snapshot", "it")
    _assert_runs(access_project, "diff", "it")
    _assert_runs(access_project, "new", "it", "scaffold", "--table", "events")
    _assert_runs(access_project, "down", "it")
    assert access_project.client.command(f"EXISTS TABLE {identity.database}.events") == 0


@pytest.mark.parametrize(
    "dropped, command, denied",
    [
        ("SELECT ON system.mutations", "up", "system.mutations"),
        ("SELECT ON system.dictionaries", "snapshot", "system.dictionaries"),
    ],
)
def test_u7_these_system_grants_are_needed(access_project, monkeypatch, dropped, command, denied):
    kept = tuple(g for g in CORE_REQUIRED if g != dropped)
    identity = _core_migration_user(access_project, kept, monkeypatch)
    _write_migration(access_project, _u7_upgrade(identity), U7_DOWNGRADE)
    result = access_project.run(command, "it")
    assert result.exit_code != 0, result.output
    assert "ACCESS_DENIED" in result.output or "Not enough privileges" in result.output
    assert denied in result.output, result.output


def test_u7_system_databases_and_tables_are_readable_without_a_grant(access_project, monkeypatch):
    """ClickHouse grants them implicitly on self-hosted servers, so revoking changes nothing."""
    implicit = ("SELECT ON system.databases", "SELECT ON system.tables")
    kept = tuple(g for g in CORE_REQUIRED if g not in implicit)
    identity = _core_migration_user(access_project, kept, monkeypatch)
    _write_migration(access_project, _u7_upgrade(identity), U7_DOWNGRADE)
    _assert_runs(access_project, "up", "it")
    _assert_runs(access_project, "down", "it")


@pytest.mark.cluster
@pytest.mark.parametrize(
    "dropped, named",
    [
        ("CLUSTER ON *.*", "CLUSTER"),
        ("READ ON REMOTE", "READ ON REMOTE"),
        ("SYSTEM SYNC REPLICA, SYSTEM SYNC DATABASE REPLICA ON {db}.*", "SYSTEM SYNC REPLICA"),
    ],
)
def test_u7_cluster_up_needs_these_best_effort_grants(
    cluster_access_project, monkeypatch, dropped, named
):
    """With `cluster` set, `up` fails without each of these, and the error names it."""
    project = cluster_access_project
    grants = CORE_REQUIRED + tuple(g for g in CORE_BEST_EFFORT if g != dropped)
    _cluster_migration_user(project, grants, monkeypatch)
    _write_migration(project, U7_CLUSTER_UPGRADE, U7_CLUSTER_DOWNGRADE)
    result = project.run("up", "it")
    assert result.exit_code != 0, result.output
    assert "ACCESS_DENIED" in result.output and named in result.output, result.output


@pytest.mark.cluster
@pytest.mark.parametrize("dropped", [None, "CREATE TEMPORARY TABLE ON *.*"])
def test_u7_cluster_up_runs_without_create_temporary_table(
    cluster_access_project, monkeypatch, dropped
):
    project = cluster_access_project
    grants = CORE_REQUIRED + tuple(g for g in CORE_BEST_EFFORT if g != dropped)
    _cluster_migration_user(project, grants, monkeypatch)
    _write_migration(project, U7_CLUSTER_UPGRADE, U7_CLUSTER_DOWNGRADE)
    _assert_runs(project, "up", "it")
    _assert_runs(project, "down", "it")


@pytest.mark.cluster
@pytest.mark.parametrize(
    "dropped, expected",
    [
        (None, "Timed out"),
        ("SELECT ON system.parts", "Timed out"),
        ("SELECT ON system.replicas", "system.replicas"),
    ],
)
def test_u7_cluster_waiting_on_a_held_mutation(
    cluster_access_project, clickhouse_cluster, monkeypatch, dropped, expected
):
    """The waiter reads system.replicas, and maybe system.parts, only while a mutation is pending.

    Without system.parts it still waits; without system.replicas it fails. Merges stay
    stopped, so a waiter that works ends at `up`'s timeout.
    """
    project = cluster_access_project
    grants = CORE_REQUIRED + tuple(g for g in CORE_BEST_EFFORT if g != dropped)
    _cluster_migration_user(project, grants, monkeypatch)
    _hold_cluster_mutations(project, clickhouse_cluster)
    _write_migration(project, U7_HELD_UPGRADE, U7_CLUSTER_DOWNGRADE)
    result = project.run("up", "it", "--timeout", "2")
    assert result.exit_code == 1 and expected in result.output, result.output
    if dropped == "SELECT ON system.replicas":
        assert "ACCESS_DENIED" in result.output and "Timed out" not in result.output


# U8. system.privileges on each image.


def test_u8_system_privileges_shape_on_this_image(access_project):
    client = access_project.client
    image = _image(client)
    columns = client.query("SELECT * FROM system.privileges LIMIT 0").column_names
    assert list(columns) == ["privilege", "aliases", "level", "parent_group"]
    tree = PrivilegeTree.read(client)
    assert len(tree.levels) > 150
    assert tree.levels["CREATE USER"] == image["create_user_level"]
    assert tree.canonical("DELETE") == "ALTER DELETE"
    assert ("READ" in tree.levels) == image["source_grants"]
    grant_columns = client.query("SELECT * FROM system.grants LIMIT 0").column_names
    assert ("access_object" in grant_columns) == image["source_grants"]


def test_u8_leaf_expansion_matches_both_profiles_on_this_image(access_project):
    """U1's mechanism on this image, without READ ON REMOTE where the image can't parse it."""
    client = access_project.client
    role = access_project.name + "_migration_role"
    for profile in (_legacy_grants(access_project), _core_grants(access_project)):
        profile = _given_in_full(client, profile)
        _reset_role(client, role)
        kept = []
        for statement in profile:
            error = _try(client, _as_written(statement, role))
            if error:
                assert not _source_grants(client) and "REMOTE" in statement, error
                assert "SYNTAX_ERROR" in error
                continue
            kept.append(statement)
        _assert_in_sync(client, role, kept)


def test_u8_legacy_bootstrap_on_this_image(access_project):
    """0.8's bootstrap ended at READ ON REMOTE, which 24.8 can't parse, before any user existed.

    T6 changed that on purpose (design note, Decision 6): the user is created before any
    grant, and READ ON REMOTE is best-effort, so on 24.8 its SYNTAX_ERROR is only a warning.
    """
    result = access_project.run("bootstrap", "it")
    users = access_project.client.command(
        "SELECT count() FROM system.users WHERE name = {name:String}",
        parameters={"name": access_project.migration_user},
    )
    assert result.exit_code == 0 and users == 1, result.output
    if not _source_grants(access_project.client):
        assert "READ ON REMOTE" in result.stderr and "SYNTAX_ERROR" in result.stderr


# U9 and U10. Where users and roles are stored, and when other nodes see them.


@pytest.mark.cluster
def test_u9_local_storage_is_detected_and_needs_on_cluster(clickhouse_cluster, request):
    cluster = clickhouse_cluster
    name = "it_" + secrets.token_hex(8)
    request.addfinalizer(lambda: _drop_everywhere(cluster, name))
    assert _writable_storage(cluster.clients[1]) == "local_directory"
    cluster.clients[1].command(f"CREATE ROLE {name}_here")
    cluster.clients[1].command(f"CREATE ROLE {name}_everywhere ON CLUSTER {cluster.name}")
    cluster.clients[1].command(
        f"GRANT ON CLUSTER {cluster.name} SELECT ON system.one TO {name}_everywhere"
    )
    assert _role_storage(cluster.clients[1], f"{name}_here") == "local_directory"
    assert _roles_on(cluster.clients[2], name) == {f"{name}_everywhere"}
    assert _grant_rows(cluster.clients[2], "role", f"{name}_everywhere")
    cluster.clients[1].command(f"GRANT SELECT ON system.numbers TO {name}_everywhere")
    node2 = {row["table"] for row in _grant_rows(cluster.clients[2], "role", f"{name}_everywhere")}
    assert node2 == {"one"}


@pytest.mark.cluster
def test_u9_replicated_storage_is_detected_and_reaches_every_node(
    clickhouse_cluster_keeper, request
):
    cluster = clickhouse_cluster_keeper
    name = "it_" + secrets.token_hex(8)
    request.addfinalizer(lambda: _drop_everywhere(cluster, name))
    for client in cluster.clients.values():
        assert _writable_storage(client) == "replicated"
    cluster.clients[1].command(f"CREATE ROLE {name}_here")
    assert _role_storage(cluster.clients[1], f"{name}_here") == "replicated"
    _wait_until(lambda: _roles_on(cluster.clients[2], name) == {f"{name}_here"})
    with pytest.raises(DatabaseError) as raised:
        cluster.clients[1].command(
            f"CREATE ROLE {name}_twice ON CLUSTER {cluster.name}",
            # Buffered, so the error keeps its code (see the next test).
            settings={"distributed_ddl_task_timeout": 30, "wait_end_of_query": 1},
        )
    assert (raised.value.code, raised.value.name) == (493, "ACCESS_ENTITY_ALREADY_EXISTS")


@pytest.mark.cluster
@pytest.mark.parametrize("buffered", [False, True])
def test_u9_on_cluster_error_after_a_host_answered_needs_wait_end_of_query(
    clickhouse_cluster_keeper, request, buffered
):
    """Node 2 starts late, so node 1's status row is sent before node 2's error.

    Unbuffered, the server has already answered 200: it appends the error and cuts the
    response, and the client sees a broken stream with no code. `wait_end_of_query=1` turns
    the same error into an HTTP 500 with its code.
    """
    cluster = clickhouse_cluster_keeper
    name = "it_" + secrets.token_hex(8)
    request.addfinalizer(lambda: _drop_everywhere(cluster, name))
    error = _create_role_on_cluster_with_node_2_late(cluster, name, buffered)
    if buffered:
        assert (error.code, error.name) == (493, "ACCESS_ENTITY_ALREADY_EXISTS"), error
    else:
        assert isinstance(error, OperationalError) and error.code is None, error
        assert "Connection broken" in str(error), error


@pytest.mark.cluster
def test_u10_replicated_grant_reaches_the_other_node_within_the_retry_limit(
    clickhouse_cluster_keeper, request
):
    """Measured on Docker: printed for the spike record; asserted only against 30 s."""
    cluster = clickhouse_cluster_keeper
    name = "it_" + secrets.token_hex(8)
    request.addfinalizer(lambda: _drop_everywhere(cluster, name))
    cluster.clients[1].command(f"CREATE ROLE {name}_role")
    _wait_until(lambda: _roles_on(cluster.clients[2], name) == {f"{name}_role"})
    delays = []
    for table in ("one", "numbers", "tables", "databases", "columns") * 4:
        cluster.clients[1].command(f"GRANT SELECT ON system.{table} TO {name}_role")
        delays.append(_time_until_visible(cluster.clients[2], f"{name}_role", table))
        cluster.clients[1].command(f"REVOKE SELECT ON system.{table} FROM {name}_role")
    print(f"U10 delays (s): max {max(delays):.3f}, median {statistics.median(delays):.3f}")
    assert max(delays) < 30


@pytest.fixture
def cluster_access_project(clickhouse_cluster, tmp_path, monkeypatch, request):
    """A project on the local-storage cluster whose migration user is made by the test."""
    cluster = clickhouse_cluster
    project = _cluster_project(cluster, tmp_path, monkeypatch)
    request.addfinalizer(lambda: _drop_everywhere(cluster, project.name))
    request.addfinalizer(
        lambda: cluster.clients[1].command(
            f"DROP DATABASE IF EXISTS {project.database} ON CLUSTER {cluster.name} SYNC"
        )
    )
    cluster.clients[1].command(f"CREATE DATABASE {project.database} ON CLUSTER {cluster.name}")
    return project


# Helpers.


def _grant_to_fresh_role(client, grant, role):
    """Grant to an empty role; return the server's error text, or None."""
    _reset_role(client, role)
    return _try(client, _grant_sql(grant, role))


def _reset_role(client, role):
    client.command(f"DROP ROLE IF EXISTS {role}")
    client.command(f"CREATE ROLE {role}")


def _grant_sql(grant, role, on_cluster=""):
    """`GRANT [ON CLUSTER c] [CURRENT GRANTS(]... ON target[)] TO role [WITH GRANT OPTION]`."""
    what = f"{', '.join(grant.privileges)} ON {_target_sql(grant.scope)}"
    if grant.best_effort:
        what = f"CURRENT GRANTS({what})"
    cluster = f" ON CLUSTER {on_cluster}" if on_cluster else ""
    option = " WITH GRANT OPTION" if grant.grant_option else ""
    return f"GRANT{cluster} {what} TO {role}{option}"


def _as_written(statement, role):
    """0.8's statements already name the role; a core grant is `<privileges> ON <target>`."""
    return statement if statement.startswith("GRANT") else f"GRANT {statement} TO {role}"


def _target_sql(scope):
    if scope.source:
        return scope.source
    return f"{scope.database or '*'}.{scope.table or '*'}"


def _try(client, sql):
    try:
        client.command(sql)
    except DatabaseError as error:
        return str(error)
    return None


def _grant_rows(client, kind, name):
    column = "user_name" if kind in ("user", "user_name") else "role_name"
    result = client.query(
        f"SELECT * FROM system.grants WHERE {column} = {{name:String}}",
        parameters={"name": name},
    )
    return [dict(zip(result.column_names, row)) for row in result.result_rows]


def _row_key(row):
    return (row["access_type"], row["database"], row["table"], row.get("access_object", ""))


def _flagged_rows(client, role):
    return {
        (
            row["access_type"],
            row["database"],
            row["table"],
            row["grant_option"],
            row["is_partial_revoke"],
        )
        for row in _grant_rows(client, "role", role)
    }


def _assert_replace_converges(project, role, desired):
    """Back to `db.*` plus a membership, then one REPLACE statement to the desired set."""
    client, db = project.client, project.database
    member = project.name + "_member"
    client.command(f"CREATE ROLE IF NOT EXISTS {member}")
    client.command(f"GRANT {member} TO {role}")
    client.command(f"REVOKE ALL ON *.* FROM {role}")
    client.command(f"GRANT SELECT ON {db}.* TO {role}")
    client.command(f"GRANT {', '.join(desired)} TO {role} WITH REPLACE OPTION")
    _assert_in_sync(client, role, desired)
    memberships = client.query(
        "SELECT granted_role_name FROM system.role_grants WHERE role_name = {r:String}",
        parameters={"r": role},
    ).result_rows
    assert memberships == [(member,)]


def _difference(client, role, statements):
    tree = PrivilegeTree.read(client)
    desired = desired_rights(tree, [parse_grant(statement) for statement in statements])
    return compare(desired, live_rights(tree, _grant_rows(client, "role", role)))


def _assert_in_sync(client, role, statements):
    difference = _difference(client, role, statements)
    assert difference.in_sync, (statements, difference)


def _assert_rejection_explained(error, desired, held_by_admin):
    """A rejected grant expands to nothing, or asks for rights the admin can't pass on: not
    held with grant option, or with part of it revoked, as ClickHouse Cloud's admin has
    `SELECT ON system.zookeeper` revoked inside `SELECT ON *.*`."""
    if "ACCESS_DENIED" in error:
        held, revoked = held_by_admin
        assert any(not held_in_full(r, held, revoked) for r in desired)
        return
    assert "INVALID_GRANT" in error or "SYNTAX_ERROR" in error, error
    assert not desired, error


def _assert_source_rewrite(grant, difference):
    """`<SOURCE> ON *.*` is stored as READ and WRITE on that source (SOURCES: on all)."""
    [privilege] = grant.privileges
    assert grant.scope == Scope() and not difference.partial_revokes, (grant, difference)
    assert difference.missing == {Right(privilege, Scope(), False)}, (grant, difference)
    source = Scope() if privilege == "SOURCES" else Scope(source=privilege)
    expected = {Right("READ", source, False), Right("WRITE", source, False)}
    assert difference.extra == expected, (grant, difference)


def _create_events_table(project):
    project.client.command(f"CREATE DATABASE IF NOT EXISTS {project.database}")
    project.client.command(
        f"CREATE TABLE IF NOT EXISTS {project.database}.events (id UInt64) "
        "ENGINE = MergeTree ORDER BY id"
    )


def _given_in_full(client, statements):
    """The statements whose rights the admin can give in full: all of them on a stock server.

    A best-effort `CURRENT GRANTS` lands only what the admin holds with grant option, so it
    is compared only where that is all of it. ClickHouse Cloud's admin holds neither
    `SELECT ON system.zookeeper` nor a CLUSTER grant: Cloud grants CLUSTER to every login.
    """
    return [s for s in statements if not parse_grant(s).best_effort or gives_in_full(client, s)]


def _legacy_grants(project):
    """The privilege grants in 0.8's bootstrap SQL, for the project's role and database."""
    sql = build_bootstrap_sql(
        project=project.name,
        db=project.database,
        migration_user=project.migration_user,
        migration_password="unused",
    )
    lines = [line for line in sql.splitlines() if not line.strip().startswith("--")]
    statements = [" ".join(s.split()) for s in "\n".join(lines).split(";")]
    return [s for s in statements if s.startswith("GRANT") and " ON " in s]


def _core_grants(project):
    return _render(project, CORE_REQUIRED + CORE_BEST_EFFORT)


def _render(project, grants):
    return [grant.format(db=project.database) for grant in grants]


def _identity(project, tag):
    name = f"{project.name}_{tag}"
    return Identity(project.database, name + "_role", name, AccessProject.new_password())


def _create_user(client, identity):
    client.command(
        f"CREATE USER {identity.user} IDENTIFIED BY {{password:String}}",
        parameters={"password": identity.password},
    )


def _connect(server, identity, request):
    client = replace(server, user=identity.user, password=identity.password).connect()
    request.addfinalizer(client.close)
    return client


def _admin_views(client, user):
    """Everything an admin reads by default that could carry a user's password."""
    secrets_shown = {"format_display_secrets_in_show_and_select": 1}
    texts = [
        _text(client, f"SHOW CREATE USER {user}", None),
        _text(client, f"SHOW CREATE USER {user}", secrets_shown),
        _text(client, f"SELECT * FROM system.users WHERE name = '{user}'", None),
    ]
    if client.command("CHECK GRANT SHOW ACCESS ON *.*"):  # ClickHouse Cloud's admin can't
        texts.append(_text(client, "SHOW ACCESS", None))
    logged = client.query(
        f"SELECT query FROM {query_log(client)} WHERE position(query, {{user:String}}) > 0",
        parameters={"user": user},
    ).result_rows
    assert logged, "the CREATE USER statement should be in the query log"
    return texts + [str(row) for row in logged]


def _text(client, sql, settings):
    return repr(client.query(sql, settings=settings).result_rows)


def _password_forms(password):
    """The password and the digests ClickHouse can store for it, in lowercase hex."""
    sha1 = hashlib.sha1(hashlib.sha1(password.encode()).digest()).hexdigest()
    return [password.lower(), hashlib.sha256(password.encode()).hexdigest(), sha1]


def _image(client):
    version = _image_version(client)
    if version not in IMAGES:
        pytest.skip(f"No spike expectations for ClickHouse {version}; record them in IMAGES")
    return IMAGES[version]


def _image_version(client):
    return ".".join(client.command("SELECT version()").split(".")[:2])


def _source_grants(client):
    """Whether the server takes `READ ON <source>`: `READ` is one of its privileges (U8)."""
    return "READ" in PrivilegeTree.read(client).levels


def _admin_login(project, server, grants):
    """A login holding only `grants`."""
    login = replace(server, user=project.name + "_admin", password=project.new_password())
    project.client.command(f"DROP USER IF EXISTS {login.user}")
    project.client.command(
        f"CREATE USER {login.user} IDENTIFIED BY {{password:String}}",
        parameters={"password": login.password},
    )
    for grant in grants:
        grant = parse_grant(grant.format(db=project.database))
        project.client.command(_grant_sql(grant, login.user))
    return login


def _core_bootstrap(admin, identity):
    """What a core-only bootstrap runs, by hand: there is no product code for it yet."""
    admin.command(f"CREATE DATABASE IF NOT EXISTS {identity.database}")
    admin.command(f"CREATE ROLE IF NOT EXISTS {identity.role}")
    for grant in CORE_REQUIRED:
        admin.command(f"GRANT {grant.format(db=identity.database)} TO {identity.role}")
    for grant in CORE_BEST_EFFORT:
        rendered = grant.format(db=identity.database)
        admin.command(f"GRANT CURRENT GRANTS({rendered}) TO {identity.role}")
    admin.command(
        f"CREATE USER IF NOT EXISTS {identity.user} IDENTIFIED BY {{password:String}}",
        parameters={"password": identity.password},
    )
    admin.command(f"GRANT {identity.role} TO {identity.user}")


def _core_migration_user(project, grants, monkeypatch):
    """The project's migration user, holding a role with exactly `grants`."""
    client = project.client
    identity = _identity(project, "migration")
    client.command(f"CREATE DATABASE IF NOT EXISTS {identity.database}")
    client.command(f"CREATE ROLE {identity.role}")
    for grant in grants:
        client.command(_grant_sql(parse_grant(grant.format(db=identity.database)), identity.role))
    _create_user(client, identity)
    client.command(f"GRANT {identity.role} TO {identity.user}")
    monkeypatch.setenv("CH_IT_MIGRATION_PASSWORD", identity.password)
    return identity


def _u7_upgrade(identity):
    """U7's migration, its dictionary reading as `identity`'s user."""
    return U7_UPGRADE.replace("<user>", identity.user).replace("<password>", identity.password)


def _write_migration(project, upgrade, downgrade):
    result = project.run("new", "it", "spike")
    assert result.exit_code == 0, result.output
    [up] = list((project.root / "migrations" / "sql").rglob("*.up.sql"))
    [down] = list((project.root / "migrations" / "sql").rglob("*.down.sql"))
    up.write_text(upgrade)
    down.write_text(downgrade)


def _assert_runs(project, *args):
    result = project.run(*args)
    assert result.exit_code == 0, (args, result.output)
    return result


def _cluster_project(cluster, tmp_path, monkeypatch):
    name = "it_" + secrets.token_hex(8)
    project = AccessProject(tmp_path, cluster.clients[1], name)
    initialize_cluster_project(cluster, project, monkeypatch)
    return project


def _cluster_migration_user(project, grants, monkeypatch):
    """Like `_core_migration_user`, with every statement ON CLUSTER so both nodes hold it."""
    client, cluster = project.client, "it_cluster"
    identity = _identity(project, "migration")
    client.command(f"CREATE ROLE {identity.role} ON CLUSTER {cluster}")
    for grant in grants:
        grant = parse_grant(grant.format(db=identity.database))
        client.command(_grant_sql(grant, identity.role, cluster))
    client.command(
        f"CREATE USER {identity.user} ON CLUSTER {cluster} IDENTIFIED BY {{password:String}}",
        parameters={"password": identity.password},
    )
    client.command(f"GRANT ON CLUSTER {cluster} {identity.role} TO {identity.user}")
    monkeypatch.setenv("CH_IT_MIGRATION_PASSWORD", identity.password)
    return identity


def _hold_cluster_mutations(project, cluster):
    """A replicated table with one row, and merges stopped on both nodes."""
    table = f"{project.database}.events"
    project.client.command(
        f"CREATE TABLE {table} ON CLUSTER {cluster.name} (id UInt64, status String) "
        f"ENGINE = ReplicatedMergeTree('/clickhouse/tables/{project.database}/events', "
        "'{replica}') ORDER BY id"
    )
    project.client.command(f"INSERT INTO {table} VALUES (1, 'new')")
    for client in cluster.clients.values():
        client.command(f"SYSTEM STOP MERGES {table}")


def _drop_everywhere(cluster, prefix):
    for client in cluster.clients.values():
        for kind, table in (("USER", "system.users"), ("ROLE", "system.roles")):
            for (name,) in client.query(
                f"SELECT name FROM {table} WHERE startsWith(name, {{p:String}})",
                parameters={"p": prefix},
            ).result_rows:
                client.command(f"DROP {kind} IF EXISTS {name}")


def _writable_storage(client):
    """The first storage CREATE ROLE writes to: users.xml is read-only."""
    return client.command(
        "SELECT type FROM system.user_directories WHERE type != 'users_xml' "
        "ORDER BY precedence LIMIT 1"
    )


def _role_storage(client, role):
    return client.command(
        "SELECT storage FROM system.roles WHERE name = {r:String}", parameters={"r": role}
    )


def _roles_on(client, prefix):
    rows = client.query(
        "SELECT name FROM system.roles WHERE startsWith(name, {p:String})",
        parameters={"p": prefix},
    ).result_rows
    return {name for (name,) in rows}


def _create_role_on_cluster_with_node_2_late(cluster, name, buffered):
    """`CREATE ROLE ... ON CLUSTER` on node 1 while node 2 is down for 3 s; return its error."""
    settings = {"distributed_ddl_task_timeout": 60, "wait_end_of_query": int(buffered)}
    client = cluster.servers[1].connect()
    errors = []

    def create():
        try:
            client.command(f"CREATE ROLE {name} ON CLUSTER {cluster.name}", settings=settings)
        except DatabaseError as error:
            errors.append(error)

    creating = threading.Thread(target=create)
    cluster.stop_node(2)
    try:
        creating.start()
        time.sleep(3)
    finally:
        cluster.start_node(2)
        creating.join(60)
        client.close()
    assert len(errors) == 1, "node 2 should fail the create: node 1 already wrote the role"
    return errors[0]


def _wait_until(condition, seconds=30):
    deadline = time.monotonic() + seconds
    while not condition():
        if time.monotonic() > deadline:
            pytest.fail(f"Not true within {seconds} seconds")
        time.sleep(0.01)


def _time_until_visible(client, role, table):
    started = time.monotonic()
    _wait_until(lambda: any(row["table"] == table for row in _grant_rows(client, "role", role)))
    return time.monotonic() - started
