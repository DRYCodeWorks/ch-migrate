"""access.yaml (2.3): every rule, with the text of each validation error.

Each error names the file, the key path and the reason. `validate` lists them all.
"""

import pytest

from ch_migrate.access.loader import EMPTY_ACCESS_FILE, declares_access
from ch_migrate.access.model import Grant, Role, Secret, Target, User

DESIGN_NOTE_EXAMPLE = """\
    # Desired users, roles and grants.
    migration_role:
      profile: core
      grants:
        - CREATE FUNCTION, DROP FUNCTION ON *.*

    roles:
      analyst:
        grants:
          - SELECT, SHOW TABLES ON {db}.*
      loader:
        grants:
          - INSERT, SELECT ON {db}.events
      reporting:
        roles: [analyst]

    users:
      dashboards:
        password: {secret: dashboards_password}
        roles: [reporting]
      etl:
        password: {secret: etl_password}
        roles: [loader]
        default_roles: [loader]
        grants:
          - SELECT ON {db}.regions

    environments:
      production:
        users:
          etl: {absent: true}
    """


def errors(result):
    return [str(problem) for problem in result.errors]


def warnings(result):
    return [str(problem) for problem in result.warnings]


def test_the_design_note_example_is_valid(validate):
    result = validate(DESIGN_NOTE_EXAMPLE)
    assert (errors(result), warnings(result)) == ([], [])
    spec = result.spec
    assert spec.migration.profile.name == "core"
    assert spec.migration.grants == (Grant(("CREATE FUNCTION", "DROP FUNCTION"), Target()),)
    assert spec.roles == (
        Role("analyst", (Grant(("SELECT", "SHOW TABLES"), Target("shop_dev")),)),
        Role("loader", (Grant(("INSERT", "SELECT"), Target("shop_dev", "events")),)),
        Role("reporting", roles=("analyst",)),
    )
    assert spec.users == (
        User("dashboards", Secret("dashboards_password"), roles=("reporting",)),
        User(
            "etl",
            Secret("etl_password"),
            grants=(Grant(("SELECT",), Target("shop_dev", "regions")),),
            roles=("loader",),
            default_roles=("loader",),
        ),
    )


def test_an_environment_block_applies_only_to_its_environment(validate):
    production = validate(DESIGN_NOTE_EXAMPLE, environment="production").spec
    assert [(user.name, user.absent) for user in production.users] == [
        ("dashboards", False),
        ("etl", True),
    ]
    etl = production.users[1]
    assert etl.roles == ("loader",)  # the base's fields stay; absent is added
    assert production.roles[0].grants[0].target == Target("shop_prod")


@pytest.mark.parametrize("text", ["", "# only a comment\n", "\n\n"])
def test_an_empty_file_is_an_error(validate, text):
    """It would read as core with nothing declared, though an empty
    access.yaml, or one `access show ENV > access.yaml` emptied, means legacy or a lost file."""
    result = validate(text)
    assert result.spec is None
    assert errors(result) == [f"access.yaml: {EMPTY_ACCESS_FILE}"]


def test_a_file_naming_only_the_profile_declares_nothing_else(validate):
    result = validate("migration_role: {profile: core}\n")
    assert errors(result) == []
    assert (result.spec.roles, result.spec.users, result.spec.legacy) == ((), (), False)
    assert result.spec.migration.profile.name == "core"


@pytest.mark.parametrize(("text", "declares"), [(None, False), ("migration_role: {}\n", True)])
def test_declares_access_follows_validates_rule(tmp_path, text, declares):
    if text is not None:
        (tmp_path / "access.yaml").write_text(text)
    assert declares_access(tmp_path) is declares


def test_declares_access_names_a_file_it_cant_read(tmp_path):
    (tmp_path / "access.yaml").write_text("roles: [unclosed\n")
    with pytest.raises(ValueError, match="access.yaml can't be read"):
        declares_access(tmp_path)


def test_declares_access_refuses_an_empty_file(tmp_path):
    (tmp_path / "access.yaml").write_text("# nothing\n")
    with pytest.raises(ValueError, match="access.yaml is empty"):
        declares_access(tmp_path)


def test_the_legacy_profile_can_be_chosen(validate):
    result = validate("migration_role: {profile: legacy}\n")
    assert result.spec.migration.profile.name == "legacy"


@pytest.mark.parametrize(
    "text, error",
    [
        (
            "grants: []\n",
            "access.yaml: grants: unknown key; the keys here are "
            "migration_role, roles, users or environments",
        ),
        (
            "roles: {analyst: {grant: []}}\n",
            "access.yaml: roles.analyst.grant: unknown key; "
            "the keys here are grants, roles or absent",
        ),
        (
            "users: {etl: {role: []}}\n",
            "access.yaml: users.etl.role: unknown key; "
            "the keys here are password, roles, default_roles, grants or absent",
        ),
        (
            "migration_role: {profiles: core}\n",
            "access.yaml: migration_role.profiles: "
            "unknown key; the keys here are profile or grants",
        ),
        (
            "roles: {a: {grants: [{grant: SELECT ON x.*, why: no}]}}\n",
            "access.yaml: roles.a.grants[0].why: unknown key; the keys here are grant or best_effort",
        ),
        (
            "environments: {dev: {environments: {}}}\n",
            "access.yaml: environments.dev.environments: "
            "unknown key; the keys here are migration_role, roles or users",
        ),
    ],
)
def test_unknown_keys_are_errors_naming_the_key_path(validate, text, error):
    assert errors(validate(text)) == [error]


@pytest.mark.parametrize(
    "text, error",
    [
        ("- roles\n", "access.yaml: expected a mapping, found a list"),
        ("roles: [analyst]\n", "access.yaml: roles: expected a mapping of names, found a list"),
        ("roles: {a: [SELECT ON x.*]}\n", "access.yaml: roles.a: expected a mapping, found a list"),
        (
            "roles: {a: {grants: SELECT ON x.*}}\n",
            "access.yaml: roles.a.grants: expected a list of grants, found a string",
        ),
        (
            "roles: {a: {grants: [3]}}\n",
            "access.yaml: roles.a.grants[0]: "
            "expected a grant or {grant: ..., best_effort: true}, found a number",
        ),
        (
            "roles: {a: {grants: [{best_effort: true}]}}\n",
            "access.yaml: roles.a.grants[0].grant: expected the grant as a string",
        ),
        (
            "roles: {a: {grants: [{grant: CLUSTER ON *.*, best_effort: maybe}]}}\n",
            "access.yaml: roles.a.grants[0].best_effort: expected true or false, found a string",
        ),
        (
            "roles: {a: {roles: reporting}}\n",
            "access.yaml: roles.a.roles: expected a list of role names, found a string",
        ),
        (
            "roles: {a: {absent: 1}}\n",
            "access.yaml: roles.a.absent: expected true or false, found a number",
        ),
        (
            "environments: [dev]\n",
            "access.yaml: environments: expected a mapping of environments, found a list",
        ),
    ],
)
def test_values_of_the_wrong_shape_are_errors(validate, text, error):
    assert errors(validate(text)) == [error]


def test_every_error_is_listed(validate):
    result = validate(
        """\
        roles:
          a: {grant: []}
          b: {grants: [SELECT ON x.t*]}
        users:
          c: {password: hunter2}
        """
    )
    assert errors(result) == [
        "access.yaml: roles.a.grant: unknown key; the keys here are grants, roles or absent",
        "access.yaml: roles.b.grants[0]: `SELECT ON x.t*`: "
        "prefix targets such as `x.t*` aren't supported in v1",
        "access.yaml: users.c.password: expected {secret: NAME}; access.yaml never holds a password",
    ]
    assert result.spec is None


def test_invalid_yaml_names_the_line(validate):
    result = validate("roles:\n  a: {grants: [SELECT ON {db}.*]}\n")
    [error] = errors(result)
    assert error.startswith("access.yaml: line 2, column ")
    assert "not valid YAML" in error


def test_grants_and_best_effort_entries(validate):
    result = validate(
        """\
        roles:
          ops:
            grants:
              - DELETE ON {db}.events WITH GRANT OPTION
              - {grant: CLUSTER ON *.*, best_effort: true}
              - {grant: "SELECT ON system.parts"}
        """
    )
    assert result.spec.roles[0].grants == (
        Grant(("ALTER DELETE",), Target("shop_dev", "events"), grant_option=True),
        Grant(("CLUSTER",), Target(), best_effort=True),
        Grant(("SELECT",), Target("system", "parts")),
    )


def test_grant_errors_quote_the_grant_as_written(validate):
    result = validate("roles: {a: {grants: [REMOTE ON *.*, 'SELECT(id) ON {db}.t']}}\n")
    assert errors(result) == [
        "access.yaml: roles.a.grants[0]: `REMOTE ON *.*`: "
        "this is the old spelling of a source grant: write `READ, WRITE ON REMOTE`",
        "access.yaml: roles.a.grants[1]: `SELECT(id) ON {db}.t`: "
        "column grants, such as SELECT(id), aren't supported in v1",
    ]


def test_unknown_privilege_names_are_warnings(validate):
    result = validate("roles: {a: {grants: ['SELEKT ON {db}.*']}}\n")
    assert errors(result) == []
    assert warnings(result) == [
        "access.yaml: roles.a.grants[0]: `SELEKT ON {db}.*`: ClickHouse 26.3 has no privilege "
        "named SELEKT; `access plan` checks it on the server"
    ]
    assert result.spec.roles[0].grants[0].privileges == ("SELEKT",)


def test_placeholders_are_rendered_in_names_and_targets(validate):
    result = validate(
        """\
        roles:
          '{project}_{env}_reader':
            grants: ['SELECT ON {db}.*']
        users:
          ci:
            password: {secret: ci_password}
            roles: ['{migration_role}', '{project}_{env}_reader']
        """
    )
    assert errors(result) == []
    assert result.spec.roles[0] == Role(
        "shop_dev_reader", (Grant(("SELECT",), Target("shop_dev")),)
    )
    assert result.spec.users[0].roles == ("shop_migration_role", "shop_dev_reader")


def test_an_unknown_placeholder_is_an_error(validate):
    result = validate("roles: {'{team}_reader': {}}\n")
    assert errors(result) == [
        "access.yaml: roles.{team}_reader: unknown placeholder {team}; "
        "the placeholders are {db}, {project}, {env} or {migration_role}"
    ]


@pytest.mark.parametrize(
    "text, error",
    [
        (
            "roles: {my-role: {}}\n",
            "access.yaml: roles.my-role: Invalid name: 'my-role'. "
            "Must start with letter/underscore and contain only alphanumeric/underscore.",
        ),
        (
            "users: {u: {roles: ['{env}-x']}}\n",
            "access.yaml: users.u.roles[0]: Invalid name: 'dev-x'. "
            "Must start with letter/underscore and contain only alphanumeric/underscore.",
        ),
        (
            "users: {u: {roles: [{project}]}}\n",
            "access.yaml: users.u.roles[0]: found a mapping: "
            "quote a name that starts with {, as in '{db}_x'",
        ),
        (
            "users: {u: {roles: [3]}}\n",
            "access.yaml: users.u.roles[0]: expected a name, found a number",
        ),
    ],
)
def test_every_rendered_name_must_be_a_valid_identifier(validate, text, error):
    assert error in errors(validate(text))


def test_two_names_that_render_the_same_are_an_error(validate):
    result = validate("roles: {'{project}_r': {}, shop_r: {}}\n")
    assert errors(result) == ["access.yaml: roles.shop_r: declares shop_r a second time"]


@pytest.mark.parametrize(
    "password, error",
    [
        ("hunter2", "expected {secret: NAME}; access.yaml never holds a password"),
        ("{secret: x, path: y}", "expected {secret: NAME}; access.yaml never holds a password"),
        ("{ssm: /a/b}", "expected {secret: NAME}; access.yaml never holds a password"),
    ],
)
def test_a_password_is_only_ever_a_named_secret(validate, password, error):
    result = validate(f"users: {{etl: {{password: {password}}}}}\n")
    assert errors(result) == [f"access.yaml: users.etl.password: {error}"]


def test_a_secret_name_must_be_an_identifier(validate):
    result = validate("users: {etl: {password: {secret: etl-password}}}\n")
    assert errors(result) == [
        "access.yaml: users.etl.password.secret: Invalid name: 'etl-password'. "
        "Must start with letter/underscore and contain only alphanumeric/underscore."
    ]


def test_one_direction_of_a_source_is_a_warning(validate):
    """Decision 12. Both directions in two entries are one grant; core's READ ON REMOTE isn't
    the user's to change, so the design note's example, on core, warns about nothing."""
    result = validate(
        """\
        migration_role: {grants: ['WRITE ON *.*']}
        roles:
          reader: {grants: [READ ON S3, READ ON REMOTE, {grant: WRITE ON REMOTE, best_effort: true}]}
        """
    )
    assert errors(result) == []
    setting = (
        "which the server keeps only with access_control_improvements.enable_read_write_grants "
        "on. With it off, ClickHouse's default, it holds READ and WRITE on {} together, and "
        "`plan` compares them as one grant."
    )
    assert warnings(result) == [
        f"access.yaml: migration_role.grants: `WRITE ON *.*` gives WRITE alone, {setting.format('*.*')}",
        f"access.yaml: roles.reader.grants: `READ ON S3` gives READ alone, {setting.format('S3')}",
    ]


def test_a_user_without_a_password_is_a_warning(validate):
    result = validate("users: {etl: {roles: [loader]}, old: {absent: true}}\n")
    assert errors(result) == []
    assert warnings(result) == [
        "access.yaml: users.etl: no password, so it is never created and must already exist. "
        "Set `password: {secret: NAME}` to have it created."
    ]


def test_memberships_on_users_and_roles(validate):
    result = validate(
        """\
        roles:
          reporting: {roles: [analyst, finance]}
        users:
          viewer: {password: {secret: viewer_password}, roles: [reporting]}
        """
    )
    assert errors(result) == []  # analyst and finance aren't declared: `plan` checks the server
    assert result.spec.roles[0].roles == ("analyst", "finance")
    assert result.spec.users[0].default_roles is None  # every membership is a default role


def test_default_roles_must_be_among_the_users_roles(validate):
    result = validate(
        """\
        users:
          etl: {password: {secret: p}, roles: [loader], default_roles: [loader, auditor, ops]}
          ci: {password: {secret: p}, default_roles: [loader]}
        """
    )
    assert errors(result) == [
        "access.yaml: users.etl.default_roles: auditor, ops aren't in roles",
        "access.yaml: users.ci.default_roles: loader isn't in roles",
    ]


def test_an_empty_default_roles_list_means_none(validate):
    result = validate("users: {etl: {password: {secret: p}, roles: [a], default_roles: []}}\n")
    assert result.spec.users[0].default_roles == ()


def test_nothing_may_hold_a_role_declared_absent(validate):
    result = validate(
        """\
        roles:
          old: {absent: true}
          new: {roles: [old]}
        users:
          etl: {password: {secret: p}, roles: [old]}
          gone: {absent: true, roles: [old]}
        """
    )
    assert errors(result) == [
        "access.yaml: roles.new.roles: old is declared absent, so nothing can hold it",
        "access.yaml: users.etl.roles: old is declared absent, so nothing can hold it",
    ]


def test_absent_marks_a_user_or_role_to_drop(validate):
    result = validate("roles: {old: {absent: true}}\nusers: {gone: {absent: true}}\n")
    assert result.spec.roles == (Role("old", absent=True),)
    assert result.spec.users == (User("gone", absent=True),)


def test_environment_blocks_deep_merge_mappings_and_replace_lists(validate):
    text = """\
        roles:
          analyst: {grants: ['SELECT ON {db}.*'], roles: [base]}
        environments:
          dev:
            roles:
              analyst: {grants: ['SELECT ON {db}.events']}
              extra: {}
        """
    result = validate(text)
    analyst, extra = result.spec.roles
    assert analyst.grants == (Grant(("SELECT",), Target("shop_dev", "events")),)  # replaced
    assert analyst.roles == ("base",)  # kept: the block didn't set it
    assert extra == Role("extra")
    assert validate(text, environment="production").spec.roles == (
        Role("analyst", (Grant(("SELECT",), Target("shop_prod")),), roles=("base",)),
    )


def test_absent_in_an_environment_block_drops_only_there(validate):
    text = "users:\n  etl: {password: {secret: p}}\nenvironments:\n  dev:\n    users: {etl: {absent: true}}\n"
    assert validate(text).spec.users[0].absent
    assert not validate(text, environment="production").spec.users[0].absent


def test_the_migration_role_can_change_per_environment(validate):
    text = (
        "migration_role: {grants: [CREATE FUNCTION ON *.*]}\n"
        "environments: {dev: {migration_role: {profile: legacy}}}\n"
    )
    dev = validate(text).spec.migration
    assert (dev.profile.name, dev.grants) == ("legacy", (Grant(("CREATE FUNCTION",), Target()),))
    assert validate(text, environment="production").spec.migration.profile.name == "core"


def test_an_environment_config_yaml_lacks_is_an_error(validate):
    result = validate("environments: {prod: {users: {}}}\n")
    assert errors(result) == [
        "access.yaml: environments.prod: config.yaml has no environment named prod"
    ]


def test_errors_in_another_environments_block_are_still_reported(validate):
    result = validate("environments: {production: {roles: {a: {grant: []}}}}\n")
    assert errors(result) == [
        "access.yaml: environments.production.roles.a.grant: "
        "unknown key; the keys here are grants, roles or absent"
    ]


def test_an_unknown_profile_is_an_error(validate):
    result = validate("migration_role: {profile: full}\n")
    assert errors(result) == [
        "access.yaml: migration_role.profile: unknown profile 'full'; the profiles are core and legacy"
    ]


@pytest.mark.parametrize(
    "text, error",
    [
        (
            "roles: {shop_migration_role: {}}\n",
            "access.yaml: roles.shop_migration_role: "
            "this is the migration role: add its grants under migration_role.grants",
        ),
        (
            "roles: {'{migration_role}': {absent: true}}\n",
            "access.yaml: roles.{migration_role}: "
            "this is the migration role: add its grants under migration_role.grants",
        ),
        (
            "users: {shop_migrator: {}}\n",
            "access.yaml: users.shop_migrator: "
            "this is the migration user, which `bootstrap` manages",
        ),
        (
            "users: {shop_admin: {}}\n",
            "access.yaml: users.shop_admin: "
            "this is config.yaml's admin_user, which ch-migrate never changes",
        ),
    ],
)
def test_the_migration_identity_and_the_admin_cannot_be_declared(validate, text, error):
    assert errors(validate(text)) == [error]


def test_an_error_after_a_merge_names_the_environment_block(validate):
    text = (
        "users:\n  etl: {password: {secret: p}, roles: [a], default_roles: [a]}\n"
        "environments:\n  dev:\n    users: {etl: {roles: [b]}}\n"
    )
    assert errors(validate(text)) == ["access.yaml: users.etl.default_roles: a isn't in roles"]
    assert errors(validate(text.replace("default_roles: [a]", ""))) == []
    moved = text.replace("default_roles: [a]}", "}").replace(
        "roles: [b]}", "roles: [b], default_roles: [a]}"
    )
    assert errors(validate(moved)) == [
        "access.yaml: environments.dev.users.etl.default_roles: a isn't in roles"
    ]
