"""The planner: one declaration and one read of the server in, the plan's lines out.

No server here: each test builds what the server holds by hand, on the bundled 26.3
privilege tree. `tests/integration/test_access_plan.py` runs the same rules on Docker.
"""

from dataclasses import replace

import pytest

from ch_migrate.access.grants import parse_grant
from ch_migrate.access.live import LiveEntity, LiveState, Membership
from ch_migrate.access.planner import build_plan, wanted
from ch_migrate.access.privileges import bundled_tree
from ch_migrate.access.rights import Holdings, Scope, StoredRow

TREE = bundled_tree()
MIGRATION_ROLE, MIGRATION_USER = "shop_migration_role", "shop_migrator"


def holding(*grants, roles=(), revoked=()):
    """A user or role that holds these grants, partial revokes and roles."""
    rows = [flag_row for grant in grants for flag_row in stored(grant)]
    rows += [flag_row for grant in revoked for flag_row in stored(grant, revoke=True)]
    memberships = tuple(
        role if isinstance(role, Membership) else Membership(role) for role in roles
    )
    return LiveEntity(Holdings(tuple(rows)), memberships)


def stored(text, revoke=False):
    grant = parse_grant(text)
    scope = Scope.of(grant.target)
    flags = [
        flag for name in grant.privileges for flag in TREE.flags(TREE.canonical(name), scope.kind)
    ]
    return [StoredRow(flag, scope, grant.grant_option, revoke) for flag in sorted(flags)]


def server(spec, roles=None, users=None):
    """A server whose migration role and user match `spec`, plus these roles and users."""
    migration = spec.migration
    in_sync = holding(*(str(grant) for grant in migration.all_grants))
    return LiveState(
        TREE,
        "local_directory",
        {migration.user: holding(roles=(migration.role,)), **(users or {})},
        {migration.role: in_sync, **(roles or {})},
        frozenset({"REMOTE"}),
    )


def lines(plan):
    return [f"{change.marker} {change}" for change in plan.changes]


def planned(validate, text, **held):
    spec = validate(text).spec
    return build_plan(spec, server(spec, **held))


ROLE = "roles: {analyst: {grants: ['SELECT, SHOW TABLES ON {db}.*']}}"


def test_in_sync_has_no_lines_and_exits_0(validate):
    held = holding("SHOW TABLES ON shop_dev.*", "SELECT ON shop_dev.* ")
    plan = planned(validate, ROLE, roles={"analyst": held})
    assert (lines(plan), plan.shortfalls, plan.errors) == ([], (), ())
    assert (plan.exit_code, plan.missing_rows) == (0, False)


def test_nothing_on_the_server_is_all_creates_and_grants_in_apply_order(validate):
    spec = validate(
        """\
        roles:
          analyst: {grants: ['SELECT ON {db}.*']}
          reporting: {roles: [analyst]}
        users:
          etl:
            password: {secret: etl_password}
            roles: [reporting, analyst]
            default_roles: [reporting]
            grants: ['INSERT ON {db}.events']
        """
    ).spec
    plan = build_plan(spec, LiveState(TREE, sources=frozenset({"REMOTE"})))
    assert [line for line in lines(plan) if MIGRATION_ROLE not in line] == [
        "+ create role analyst",
        "+ create role reporting",
        "+ create user shop_migrator",
        "+ create user etl",
        "+ grant role analyst to role reporting",
        "+ grant role reporting to user etl",
        "+ grant role analyst to user etl",
        "+ grant SELECT ON shop_dev.* to role analyst",
        "+ grant INSERT ON shop_dev.events to user etl",
        "+ set the default roles of user etl to reporting",
    ]
    assert lines(plan)[0] == f"+ create role {MIGRATION_ROLE}"
    assert (plan.exit_code, plan.missing_rows) == (1, True)
    assert len(plan.pending) == len(plan.changes)


def test_other_flags_on_the_same_scope_are_one_revoke(validate):
    held = holding("SELECT, SHOW TABLES, INSERT, ALTER UPDATE ON shop_dev.*")
    plan = planned(validate, ROLE, roles={"analyst": held})
    assert lines(plan) == [
        "~ adopt role analyst: it exists on the server, and access.yaml now manages it",
        "- revoke ALTER UPDATE, INSERT ON shop_dev.* from role analyst",
    ]
    assert (plan.exit_code, plan.missing_rows) == (1, False)


def test_narrowing_a_privilege_revokes_the_other_leaves(validate):
    text = "roles: {editor: {grants: ['ALTER UPDATE ON {db}.*']}}"
    plan = planned(validate, text, roles={"editor": holding("ALTER ON shop_dev.*")})
    [_, revoke] = plan.changes
    assert revoke.action == "revoke" and revoke.grant.target.database == "shop_dev"
    assert "ALTER" not in revoke.grant.privileges
    assert "ALTER UPDATE" not in revoke.grant.privileges
    assert "ALTER DELETE" in revoke.grant.privileges


def test_an_option_that_is_not_declared_is_revoked_alone(validate):
    held = holding("SELECT, SHOW TABLES ON shop_dev.* WITH GRANT OPTION")
    plan = planned(validate, ROLE, roles={"analyst": held})
    assert lines(plan)[1:] == [
        "- revoke the grant option for SELECT, SHOW TABLES ON shop_dev.* from role analyst"
    ]


def test_a_missing_option_is_granted(validate):
    text = "roles: {analyst: {grants: ['SELECT ON {db}.* WITH GRANT OPTION']}}"
    plan = planned(validate, text, roles={"analyst": holding("SELECT ON shop_dev.*")})
    assert lines(plan)[1:] == ["+ grant SELECT ON shop_dev.* WITH GRANT OPTION to role analyst"]


@pytest.mark.parametrize(
    ("declared", "held", "instead_of"),
    [
        (
            "SELECT ON {db}.events",
            ["SELECT ON shop_dev.*"],
            "revoke SELECT ON shop_dev.* from role r",
        ),
        (
            "SELECT ON {db}.*",
            ["SELECT ON *.*"],
            "revoke SELECT ON *.* from role r",
        ),
        (
            "READ ON REMOTE",
            ["READ ON *.*"],
            "revoke READ ON *.* from role r",
        ),
        (
            "SELECT ON {db}.events WITH GRANT OPTION",
            ["SELECT ON shop_dev.* WITH GRANT OPTION"],
            "revoke SELECT ON shop_dev.* from role r",
        ),
    ],
)
def test_narrowing_a_scope_is_one_replace(validate, declared, held, instead_of):
    """The broader grant absorbs the narrower one, so it can't be a grant and a revoke."""
    plan = planned(
        validate, f"roles: {{r: {{grants: ['{declared}']}}}}", roles={"r": holding(*held)}
    )
    assert lines(plan)[1:] == [
        "- replace the grants of role r with its declared set in one statement, "
        f"instead of: {instead_of}"
    ]
    [replace] = plan.pending
    assert [change.action for change in replace.covers] == ["revoke"]


def test_an_option_on_a_broader_scope_than_a_declared_option_is_a_replace(validate):
    """Revoking the option on `db.*` would take the declared option on `db.events` too."""
    text = "roles: {r: {grants: ['SELECT ON {db}.*', 'SELECT ON {db}.events WITH GRANT OPTION']}}"
    held = holding("SELECT ON shop_dev.* WITH GRANT OPTION")
    plan = planned(validate, text, roles={"r": held})
    assert lines(plan)[1:] == [
        "- replace the grants of role r with its declared set in one statement, instead of: "
        "revoke the grant option for SELECT ON shop_dev.* from role r"
    ]


def test_a_replace_covers_the_grants_it_makes_and_everything_it_takes_away(validate):
    text = "roles: {r: {grants: ['SELECT ON {db}.events', 'INSERT ON {db}.events']}}"
    held = holding("SELECT, SHOW TABLES ON shop_dev.*", "CREATE ROLE ON *.*")
    plan = planned(validate, text, roles={"r": held})
    assert lines(plan)[1:] == [
        "- replace the grants of role r with its declared set in one statement, instead of: "
        "grant INSERT ON shop_dev.events to role r; "
        "revoke CREATE ROLE ON *.* from role r; "
        "revoke SELECT, SHOW TABLES ON shop_dev.* from role r"
    ]
    assert plan.missing_rows, "the grant it covers is a row a replica might not have yet"


def test_a_partial_revoke_inside_a_declared_grant_is_granted_again(validate):
    held = holding("SELECT, SHOW TABLES ON shop_dev.*", revoked=["SELECT ON shop_dev.secrets"])
    plan = planned(validate, ROLE, roles={"analyst": held})
    assert lines(plan)[1:] == ["+ grant SELECT ON shop_dev.* to role analyst"]


def test_a_column_grant_is_narrower_than_its_table_and_revoked_by_column(validate):
    text = "roles: {r: {grants: ['INSERT ON {db}.events']}}"
    column = StoredRow("SELECT", Scope("shop_dev", "events", "id"))
    held = LiveEntity(Holdings((*stored("INSERT ON shop_dev.events"), column)))
    plan = planned(validate, text, roles={"r": held})
    assert lines(plan)[1:] == ["- revoke SELECT(id) ON shop_dev.events from role r"]


def test_memberships_are_granted_and_revoked_and_an_admin_option_is_revoked(validate):
    text = """\
        roles: {a: {}, b: {}, c: {}}
        users: {u: {password: {secret: p}, roles: [a, b]}}
        """
    user = holding(roles=(Membership("b", admin_option=True), "c"))
    plan = planned(validate, text, roles=dict.fromkeys("abc", holding()), users={"u": user})
    assert lines(plan) == [
        "~ adopt user u: it exists on the server, and access.yaml now manages it",
        "+ grant role a to user u",
        "- revoke role c from user u",
        "- revoke the admin option for role b from user u",
    ]


def test_a_membership_may_name_an_undeclared_role_that_the_server_has(validate):
    text = "users: {u: {password: {secret: p}, roles: [outsider]}}"
    plan = planned(validate, text, roles={"outsider": holding()})
    assert lines(plan) == ["+ create user u", "+ grant role outsider to user u"]
    missing = planned(validate, text)
    assert (missing.exit_code, missing.missing_rows) == (2, True)
    assert missing.errors == (
        "user u is declared to hold role outsider, "
        "which access.yaml doesn't declare and the server doesn't have.",
    )


def test_a_user_with_no_password_is_never_created(validate):
    text = "users: {u: {grants: ['SELECT ON {db}.*']}}"
    plan = planned(validate, text)
    assert (plan.exit_code, plan.missing_rows, lines(plan)) == (2, True, [])
    assert plan.errors == (
        "user u isn't on the server and has no password, so it can't be created. "
        "Set `password: {secret: NAME}`, or create the user first.",
    )
    existing = planned(validate, text, users={"u": holding("SELECT ON shop_dev.*")})
    assert (existing.exit_code, lines(existing)) == (0, [])


def test_absent_drops_what_exists_and_ignores_what_does_not(validate):
    text = """\
        roles: {old_role: {absent: true}, never_was: {absent: true}}
        users: {old_user: {absent: true}}
        """
    plan = planned(validate, text, roles={"old_role": holding()}, users={"old_user": holding()})
    assert lines(plan) == ["- drop user old_user", "- drop role old_role"]
    assert (plan.exit_code, plan.missing_rows) == (1, False)


@pytest.mark.parametrize(
    ("declared", "held", "line"),
    [
        # Default roles ALL: the role granted now becomes a default role by itself.
        ("roles: [a, b]", holding(roles=("a",)), None),
        # An explicit list: the role granted now wouldn't be a default role.
        (
            "roles: [a, b]",
            replace(holding(roles=("a",)), default_all=False),
            "+ set the default roles of user u to all its roles",
        ),
        (
            "roles: [a, b], default_roles: [a]",
            replace(holding(roles=("a", Membership("b", False))), default_all=False),
            None,
        ),
        (
            "roles: [a, b], default_roles: [a]",
            holding(roles=("a", "b")),
            "+ set the default roles of user u to a",
        ),
        (
            "roles: [a], default_roles: []",
            holding(roles=("a",)),
            "+ set the default roles of user u to none",
        ),
        ("roles: [a, b]", replace(holding(roles=("a", "b")), default_except=("b",)), "ALL"),
    ],
)
def test_default_roles_are_set_when_they_would_not_match(validate, declared, held, line):
    text = f"roles: {{a: {{}}, b: {{}}}}\nusers: {{u: {{password: {{secret: p}}, {declared}}}}}"
    plan = planned(validate, text, roles=dict.fromkeys("ab", holding()), users={"u": held})
    found = [change for change in plan.changes if change.action == "set_default_roles"]
    if line == "ALL":
        line = "+ set the default roles of user u to all its roles"
    assert [f"{change.marker} {change}" for change in found] == ([line] if line else [])


def test_the_migration_role_is_added_to_and_never_revoked_or_replaced(validate):
    spec = validate("migration_role: {grants: ['CREATE FUNCTION ON *.*']}").spec
    live = server(spec)
    held = holding(
        *(str(grant) for grant in spec.migration.profile.grants if "mutations" not in str(grant)),
        "SELECT ON *.*",
        "INSERT ON other.* WITH GRANT OPTION",
        "DROP TABLE ON shop_dev.* WITH GRANT OPTION",
        roles=("helper",),
    )
    plan = build_plan(spec, LiveState(TREE, None, live.users, {MIGRATION_ROLE: held}, live.sources))
    assert lines(plan) == [
        f"+ grant CREATE FUNCTION ON *.* to role {MIGRATION_ROLE}",
        f"! role {MIGRATION_ROLE} holds SELECT ON *.* beyond its profile",
        f"! role {MIGRATION_ROLE} holds INSERT ON other.* WITH GRANT OPTION beyond its profile",
        f"! role {MIGRATION_ROLE} holds the grant option for DROP TABLE ON shop_dev.* "
        "beyond its profile",
        f"! role {MIGRATION_ROLE} holds role helper beyond its profile",
    ]
    assert [change.action for change in plan.pending] == ["grant"]


def test_the_nearest_row_decides_what_the_migration_role_holds(validate):
    """`SELECT ON *.*`, revoked on `system.*`, granted again on the tables core needs."""
    spec = validate("{}").spec
    tables = ("databases", "tables", "mutations", "dictionaries", "parts", "replicas")
    held = holding(
        *(str(grant) for grant in spec.migration.profile.grants),
        "SELECT ON *.*",
        *(f"SELECT ON system.{table}" for table in tables),
        revoked=["SELECT ON system.*"],
    )
    live = server(spec)
    plan = build_plan(spec, LiveState(TREE, None, live.users, {MIGRATION_ROLE: held}, live.sources))
    assert (plan.exit_code, plan.shortfalls) == (0, ())
    assert lines(plan) == [f"! role {MIGRATION_ROLE} holds SELECT ON *.* beyond its profile"]


def test_a_best_effort_shortfall_is_a_warning_and_names_the_part_missing(validate):
    spec = validate("roles: {ops: {grants: [{grant: 'CLUSTER ON *.*', best_effort: true}]}}").spec
    grants = [str(grant) for grant in spec.migration.all_grants]
    held = holding(
        *(grant for grant in grants if "REMOTE" not in grant and "SYNC" not in grant),
        "SYSTEM SYNC REPLICA ON shop_dev.*",
    )
    live = server(spec, roles={"ops": holding()})
    plan = build_plan(
        spec, LiveState(TREE, None, live.users, {**live.roles, MIGRATION_ROLE: held}, live.sources)
    )
    assert (plan.exit_code, lines(plan), plan.missing_rows) == (0, [], False)
    assert [str(shortfall) for shortfall in plan.shortfalls] == [
        f"best-effort: role {MIGRATION_ROLE} lacks SYSTEM SYNC DATABASE REPLICA ON shop_dev.*",
        f"best-effort: role {MIGRATION_ROLE} lacks READ ON REMOTE",
        "best-effort: role ops lacks CLUSTER ON *.*",
    ]


def test_a_held_best_effort_grant_is_not_surplus(validate):
    text = "roles: {ops: {grants: [{grant: 'CLUSTER ON *.*', best_effort: true}]}}"
    plan = planned(validate, text, roles={"ops": holding("CLUSTER ON *.*")})
    assert (lines(plan), plan.shortfalls) == ([], ())


@pytest.mark.parametrize(
    ("declared", "held"),
    [
        ("READ ON REMOTE", "READ, WRITE ON REMOTE"),
        ("WRITE ON REMOTE", "READ, WRITE ON REMOTE"),
        ("READ ON *.*", "READ, WRITE ON *.*"),
        ("READ ON REMOTE WITH GRANT OPTION", "READ, WRITE ON REMOTE WITH GRANT OPTION"),
    ],
)
def test_read_and_write_on_one_source_compare_as_one_grant(validate, declared, held):
    """Decision 12: where the server keeps them together, one direction declared is in sync."""
    text = f"roles: {{reader: {{grants: ['{declared}']}}}}"
    plan = planned(validate, text, roles={"reader": holding(held)})
    assert (lines(plan), plan.exit_code) == ([], 0)


@pytest.mark.parametrize(
    ("held", "expected"),
    [
        # The declared direction itself must be held; the other is never revoked.
        (("WRITE ON REMOTE",), "+ grant READ ON REMOTE to role reader"),
        # The other direction on another scope isn't the same grant.
        (("READ ON REMOTE", "WRITE ON *.*"), "- revoke WRITE ON *.* from role reader"),
        # A grant option goes with the pair, as declared.
        (
            ("READ, WRITE ON REMOTE WITH GRANT OPTION",),
            "- revoke the grant option for READ, WRITE ON REMOTE from role reader",
        ),
    ],
)
def test_the_other_direction_differs_only_where_it_isnt_the_same_grant(validate, held, expected):
    text = "roles: {reader: {grants: ['READ ON REMOTE']}}"
    plan = planned(validate, text, roles={"reader": holding(*held)})
    assert lines(plan) == [
        "~ adopt role reader: it exists on the server, and access.yaml now manages it",
        expected,
    ]


def test_the_migration_role_holding_both_directions_of_its_source_has_no_extra(validate):
    """Core's best-effort READ ON REMOTE reads back as READ and WRITE on Keeper storage."""
    spec = validate("{}").spec
    held = holding(*(str(grant) for grant in spec.migration.all_grants), "WRITE ON REMOTE")
    live = server(spec)
    plan = build_plan(spec, LiveState(TREE, None, live.users, {MIGRATION_ROLE: held}, live.sources))
    assert (lines(plan), plan.shortfalls, plan.exit_code) == ([], (), 0)


@pytest.mark.parametrize(
    ("grant", "sources", "problem"),
    [
        ("SELEKT ON {db}.*", ("REMOTE",), "the server has no privilege named SELEKT"),
        ("READ ON KAFKA", ("REMOTE",), "the server has no data source named KAFKA"),
        (
            "READ ON Distributed",
            ("REMOTE", "Distributed"),
            "the server stores Distributed under another name: "
            "write the source's own name, in upper case, such as REMOTE",
        ),
    ],
)
def test_what_the_server_cannot_grant(validate, grant, sources, problem):
    """Required, it is an error; best-effort, a shortfall that says why."""
    spec = validate(f"roles: {{r: {{grants: ['{grant}']}}}}").spec
    rendered = spec.roles[0].grants[0]
    plan = build_plan(spec, replace(server(spec), sources=frozenset(sources)))
    assert (plan.exit_code, plan.errors) == (2, (f"role r: `{rendered}`: {problem}.",))

    spec = validate(f"roles: {{r: {{grants: [{{grant: '{grant}', best_effort: true}}]}}}}").spec
    plan = build_plan(spec, replace(server(spec), sources=frozenset(sources)))
    assert (plan.exit_code, plan.errors) == (1, ())
    assert [str(shortfall) for shortfall in plan.shortfalls] == [
        f"best-effort: role r can't be given {rendered}: {problem}"
    ]


def test_legacy_mode_compares_only_the_migration_role_and_user(tmp_path, settings):
    """No access.yaml: the read-only and dictionary roles from config.yaml are left alone."""
    from ch_migrate.access.loader import validate_access

    legacy = replace(settings, mcp_user_name="shop_mcp", dict_reader_name="shop_dict")
    spec = validate_access(legacy, tmp_path).spec
    assert spec.legacy and {role.name for role in spec.roles} == {
        "shop_readonly_role",
        "shop_dict_role",
    }
    assert wanted(spec).names == (MIGRATION_ROLE, MIGRATION_USER)
    plan = build_plan(spec, server(spec))
    assert (plan.exit_code, lines(plan)) == (0, [])
    assert build_plan(spec, LiveState(TREE)).pending[0].action == "create_role"


def test_wanted_names_every_declared_user_and_role_and_each_source(validate):
    spec = validate(
        """\
        roles:
          reader: {grants: [READ ON S3, 'SELECT ON {db}.*']}
          gone: {absent: true}
        users:
          u: {password: {secret: p}, roles: [reader, outsider]}
        """
    ).spec
    assert wanted(spec).names == (MIGRATION_ROLE, MIGRATION_USER, "reader", "gone", "u", "outsider")
    assert wanted(spec).sources == ("REMOTE", "S3")
