"""`access show` without a server: what a read of the server becomes as access.yaml text."""

import pytest
import yaml

from ch_migrate.access.grants import parse_grant
from ch_migrate.access.live import LiveEntity, LiveState, Membership
from ch_migrate.access.privileges import bundled_tree
from ch_migrate.access.rights import Holdings, Scope, StoredRow
from ch_migrate.access.server import AccessError
from ch_migrate.access.show import Showing, show_access

TREE = bundled_tree()
HEADER = [
    "# What the server holds for dev, in access.yaml's form (`ch-migrate access show`).",
    "# Passwords are never shown. For ch-migrate to create a user, add its",
    "# `password: {secret: NAME}`.",
]


def holding(*grants, roles=(), extra_rows=()):
    rows = [flag_row for grant in grants for flag_row in stored(grant)]
    memberships = tuple(
        role if isinstance(role, Membership) else Membership(role) for role in roles
    )
    return LiveEntity(Holdings((*rows, *extra_rows)), memberships)


def stored(text, revoke=False):
    grant = parse_grant(text)
    scope = Scope.of(grant.target)
    return [
        StoredRow(flag, scope, grant.grant_option, revoke)
        for name in grant.privileges
        for flag in sorted(TREE.flags(TREE.canonical(name), scope.kind))
    ]


@pytest.fixture
def showing(validate, settings):
    def build(text, *names):
        return Showing(settings, validate(text).spec, names)

    return build


def test_a_role_prints_its_grants_then_its_roles(showing):
    """The project's database is `{db}`; the header says passwords are never shown."""
    role = holding(
        "SELECT, SHOW TABLES ON shop_dev.*",
        "SELECT ON shop_dev.events WITH GRANT OPTION",
        "ALTER ON other.*",
        "READ ON REMOTE",
        roles=("base",),
    )
    live = LiveState(TREE, roles={"analyst": role, "empty": holding()})
    shown = show_access(showing("{}", "analyst", "empty"), live)
    assert shown.warnings == ()
    assert shown.text.splitlines() == [
        *HEADER,
        "roles:",
        "  analyst:",
        "    grants:",
        "      - ALTER ON other.*",
        "      - READ ON REMOTE",
        "      - SELECT ON {db}.events WITH GRANT OPTION",
        "      - SELECT, SHOW TABLES ON {db}.*",
        "    roles:",
        "      - base",
        "  empty: {}",
    ]


def test_a_user_prints_its_roles_then_default_roles_when_they_are_not_all(showing):
    etl = holding(
        "INSERT ON shop_dev.events", roles=("analyst", Membership("loader", default=False))
    )
    users = {
        "etl": etl,
        "all_default": holding(roles=("analyst",)),
        "none_default": holding(roles=(Membership("analyst", default=False),)),
    }
    shown = show_access(showing("{}", *users), LiveState(TREE, users=users))
    assert shown.warnings == ()
    assert yaml.safe_load(shown.text) == {
        "users": {
            "etl": {
                "roles": ["analyst", "loader"],
                "default_roles": ["analyst"],
                "grants": ["INSERT ON {db}.events"],
            },
            "all_default": {"roles": ["analyst"]},
            "none_default": {"roles": ["analyst"], "default_roles": []},
        },
    }
    assert list(yaml.safe_load(shown.text)["users"]["etl"]) == ["roles", "default_roles", "grants"]


def test_what_access_yaml_cannot_declare_is_left_out_with_a_warning(showing):
    rows = (
        StoredRow("SELECT", Scope("shop_dev", "events", "id")),
        *stored("SELECT ON shop_dev.secrets", revoke=True),
        StoredRow("INSERT", Scope("shop_dev", "events"), grant_option=True, revoke=True),
        StoredRow("TABLE ENGINE", Scope(source="MergeTree")),
        StoredRow("READ", Scope(source="S3")),
        StoredRow("WRITE", Scope(source="S3")),
    )
    role = holding("SELECT ON shop_dev.*", extra_rows=rows)
    user = holding(roles=(Membership("r", admin_option=True),))
    shown = show_access(
        showing("{}", "r", "u"), LiveState(TREE, roles={"r": role}, users={"u": user})
    )
    assert yaml.safe_load(shown.text) == {
        "roles": {"r": {"grants": ["READ, WRITE ON S3", "SELECT ON {db}.*"]}},
        "users": {"u": {"roles": ["r"]}},
    }
    assert shown.warnings == (
        "role r holds SELECT(id) ON shop_dev.events: column grants aren't in v1, "
        "so it is left out.",
        "role r has a partial revoke of SELECT ON shop_dev.secrets, which access.yaml can't "
        "declare. `access plan` shows the grant that clears it.",
        "role r has a partial revoke of the grant option for INSERT ON shop_dev.events, "
        "which access.yaml can't declare. `access plan` shows the grant that clears it.",
        "role r holds TABLE ENGINE ON MergeTree, which access.yaml can't declare, "
        "so it is left out.",
        "user u holds role r with admin option, which access.yaml can't declare.",
    )


def test_the_migration_role_prints_its_profile_and_what_it_holds_beyond_it(showing):
    request = showing("migration_role: {profile: legacy}", "shop_migration_role")
    profile = [str(grant) for grant in request.spec.migration.profile.grants]
    role = holding(
        *profile, "CREATE DATABASE ON shop_dev.*", "SELECT ON other.*", roles=("helper",)
    )
    shown = show_access(request, LiveState(TREE, roles={"shop_migration_role": role}))
    assert yaml.safe_load(shown.text) == {
        "migration_role": {
            "profile": "legacy",
            "grants": ["CREATE DATABASE ON {db}.*", "SELECT ON other.*"],
        }
    }
    assert shown.warnings == (
        "role shop_migration_role holds role helper, which migration_role can't declare.",
    )


def test_without_names_it_covers_what_access_yaml_declares(showing):
    request = showing(
        """\
        roles: {analyst: {}, gone: {absent: true}}
        users: {etl: {password: {secret: p}}}
        """
    )
    assert request.names == request.wanted.names == ("analyst", "gone", "etl")
    assert request.expected == ("analyst", "etl"), "one declared absent needn't be there"
    live = LiveState(TREE, roles={"analyst": holding()})
    present = LiveState(TREE, roles={"analyst": holding()}, users={"etl": holding()})
    assert request.unsettled(live) and not request.unsettled(present)
    shown = show_access(request, live)
    assert yaml.safe_load(shown.text) == {"roles": {"analyst": {}}}
    assert shown.warnings == ("etl is declared but isn't on the server, so it isn't shown.",)
    still_there = LiveState(TREE, roles={"analyst": holding(), "gone": holding()})
    assert set(yaml.safe_load(show_access(request, still_there).text)["roles"]) == {
        "analyst",
        "gone",
    }
    assert show_access(showing("{}"), LiveState(TREE)).text.splitlines() == [*HEADER, "{}"]


def test_a_name_the_server_lacks_or_access_yaml_cannot_declare_is_refused(showing):
    with pytest.raises(AccessError, match="The server has no user or role named nobody."):
        show_access(showing("{}", "nobody"), LiveState(TREE))
    for name, reason in (("shop_admin", "admin_user"), ("shop_migrator", "the migration user")):
        with pytest.raises(AccessError, match=f"{name} is .*{reason}"):
            showing("{}", name).check()
    showing("{}", "analyst", "shop_migration_role").check()
