"""A key written twice in one mapping of access.yaml is an error, at any depth.

YAML keeps the last of two equal keys and drops the first silently, so two `analyst:`
entries would become one, and a later `plan` would revoke the first one's grants. Each
error names the file, the key path, the key and both of its lines.
"""

import textwrap

import pytest
import yaml
from click.testing import CliRunner

from ch_migrate.access.document import Document, parse_document
from ch_migrate.cli import main

KEEPS_LAST = "YAML would silently keep only the last"


def errors(result):
    return [str(problem) for problem in result.errors]


@pytest.mark.parametrize(
    "text, error",
    [
        pytest.param(
            "roles:\n  analyst:\n    grants: ['SELECT ON {db}.*']\n  loader: {}\n"
            "  analyst:\n    grants: ['INSERT ON {db}.events']\n",
            f"access.yaml: roles.analyst: duplicate key analyst on lines 2 and 5; {KEEPS_LAST}",
            id="role-declared-twice",
        ),
        pytest.param(
            "users:\n  etl: {password: {secret: etl_password}, roles: [loader]}\n"
            "  etl: {password: {secret: etl_password}}\n",
            f"access.yaml: users.etl: duplicate key etl on lines 2 and 3; {KEEPS_LAST}",
            id="user-declared-twice",
        ),
        pytest.param(
            "roles:\n  analyst:\n    grants: ['SELECT ON {db}.*']\n    roles: []\n"
            "    grants: ['SELECT ON {db}.events']\n",
            "access.yaml: roles.analyst.grants: "
            f"duplicate key grants on lines 3 and 5; {KEEPS_LAST}",
            id="entry-key-twice",
        ),
        pytest.param(
            "users:\n  etl: {password: {secret: etl_password}}\nenvironments:\n"
            "  production:\n    users:\n      etl: {absent: true}\n      etl: {roles: []}\n",
            "access.yaml: environments.production.users.etl: "
            f"duplicate key etl on lines 6 and 7; {KEEPS_LAST}",
            id="unselected-environment",
        ),
        (
            "roles: {}\nusers: {}\nroles: {}\n",
            f"access.yaml: roles: duplicate key roles on lines 1 and 3; {KEEPS_LAST}",
        ),
        (
            "environments:\n  production: {}\n  production: {}\n",
            "access.yaml: environments.production: "
            f"duplicate key production on lines 2 and 3; {KEEPS_LAST}",
        ),
        (
            "users:\n  etl:\n    password: {secret: a, secret: b}\n",
            "access.yaml: users.etl.password.secret: "
            f"duplicate key secret on line 3, columns 16 and 27; {KEEPS_LAST}",
        ),
        (
            "roles: {a: {grants: [{grant: CLUSTER ON *.*, grant: SELECT ON x.*}]}}\n",
            "access.yaml: roles.a.grants[0].grant: "
            f"duplicate key grant on line 1, columns 23 and 46; {KEEPS_LAST}",
        ),
    ],
)
def test_duplicates_at_any_depth(validate, text, error):
    result = validate(text)
    assert errors(result) == [error]
    assert result.spec is None


def test_each_repeat_is_named_against_the_first(validate):
    result = validate("roles:\n  r: {}\n  r: {}\n  r: {}\n")
    assert errors(result) == [
        f"access.yaml: roles.r: duplicate key r on lines 2 and 3; {KEEPS_LAST}",
        f"access.yaml: roles.r: duplicate key r on lines 2 and 4; {KEEPS_LAST}",
    ]


def test_duplicates_are_listed_with_every_other_error(validate):
    result = validate(
        """\
        roles:
          a: {grant: []}
          a: {grants: [SELECT ON x.t*]}
        """
    )
    assert errors(result) == [
        f"access.yaml: roles.a: duplicate key a on lines 2 and 3; {KEEPS_LAST}",
        "access.yaml: roles.a.grants[0]: `SELECT ON x.t*`: "
        "prefix targets such as `x.t*` aren't supported in v1",
    ]


def test_validate_exits_2_on_a_duplicate(project):
    (project / "access.yaml").write_text("roles:\n  analyst: {}\n  analyst: {}\n")
    result = CliRunner().invoke(main, ["access", "validate", "dev"])
    assert result.exit_code == 2, result.output
    assert result.stdout == ""
    assert result.stderr.splitlines() == [
        f"✗ access.yaml: roles.analyst: duplicate key analyst on lines 2 and 3; {KEEPS_LAST}",
        "✗ Invalid: 1 error.",
    ]


@pytest.mark.parametrize(
    "text",
    [
        "",
        "# nothing declared yet\n",
        "- roles\n",
        """\
        migration_role:
          profile: core
          grants: [CREATE FUNCTION, DROP FUNCTION ON *.*]
        roles:
          analyst: {grants: ['SELECT, SHOW TABLES ON {db}.*']}
          '{project}_reader': {grants: [{grant: CLUSTER ON *.*, best_effort: true}]}
        users:
          dashboards: {password: {secret: dashboards_password}, roles: [analyst]}
        environments:
          production:
            users:
              dashboards: {absent: true}
        """,
        """\
        base: &base {grants: ['SELECT ON {db}.*'], roles: [analyst]}
        roles:
          a:
            <<: *base
            grants: ['INSERT ON {db}.t']
          b: *base
        flags: {yes: on, no: off, 010: 0x10, when: 2026-11-12, '2': two, 2: dos}
        """,
    ],
)
def test_a_file_without_duplicates_parses_as_safe_load_does(text):
    text = textwrap.dedent(text)
    assert parse_document(text) == Document(yaml.safe_load(text))


@pytest.mark.parametrize(
    "text",
    [
        "roles:\n  a: {grants: [SELECT ON {db}.*]}\n",
        "? [a]\n: 1\n",
        "!!map a: 1\n",
        "a: 1\n---\nb: 2\n",
    ],
)
def test_invalid_yaml_fails_as_safe_load_does(text):
    with pytest.raises(yaml.YAMLError) as expected:
        yaml.safe_load(text)
    with pytest.raises(type(expected.value)) as raised:
        parse_document(text)
    assert str(raised.value) == str(expected.value)


def test_keys_yaml_reads_as_equal_are_one_key():
    document = parse_document("a:\n  yes: 1\n  true: 2\n")
    assert document.data == {"a": {True: 2}}
    assert [duplicate.path for duplicate in document.duplicates] == ["a.True"]


def test_a_merge_key_repeats_nothing_but_a_quoted_one_is_a_key():
    merged = parse_document("b: &b {x: 1}\nc: &c {y: 2}\na:\n  <<: *b\n  <<: *c\n  x: 3\n")
    assert (merged.data["a"], merged.duplicates) == ({"x": 3, "y": 2}, ())
    quoted = parse_document("a:\n  '<<': 1\n  '<<': 2\n")
    assert [duplicate.path for duplicate in quoted.duplicates] == ["a.<<"]


def test_an_aliased_mapping_is_reported_once():
    document = parse_document("x: &m {a: 1, a: 2}\ny: *m\n")
    assert [duplicate.path for duplicate in document.duplicates] == ["x.a"]
