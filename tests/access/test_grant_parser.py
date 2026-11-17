"""The grant syntax (2.3): `<privileges> ON <target> [WITH GRANT OPTION]`, checked offline."""

import pytest

from ch_migrate.access.grants import GrantSyntaxError, check_grant, parse_grant
from ch_migrate.access.model import Grant, Target
from ch_migrate.access.privileges import bundled_tree

TREE = bundled_tree()


@pytest.mark.parametrize(
    "text, expected",
    [
        ("SELECT ON *.*", Grant(("SELECT",), Target())),
        ("SELECT ON shop.*", Grant(("SELECT",), Target("shop"))),
        ("SELECT, INSERT ON shop.events", Grant(("SELECT", "INSERT"), Target("shop", "events"))),
        ("READ ON REMOTE", Grant(("READ",), Target(source="REMOTE"))),
        (
            "ALTER  UPDATE ,ALTER DELETE  ON shop.events   WITH GRANT OPTION",
            Grant(("ALTER UPDATE", "ALTER DELETE"), Target("shop", "events"), grant_option=True),
        ),
        ("select on shop.* with grant option", Grant(("select",), Target("shop"), True)),
    ],
)
def test_parses_privileges_target_and_grant_option(text, expected):
    assert parse_grant(text) == expected


def test_best_effort_is_carried_on_the_grant():
    assert parse_grant("CLUSTER ON *.*", best_effort=True).best_effort


@pytest.mark.parametrize(
    "text, reason",
    [
        ("   ", "the grant is empty"),
        ("GRANT SELECT ON shop.*", "write it without the leading GRANT"),
        (
            "CURRENT GRANTS(SELECT ON shop.*)",
            "write a best-effort grant as {grant: ..., best_effort: true}",
        ),
        (
            "SELECT ON shop.* TO analyst",
            "drop the TO clause: it goes to the user or role it's under",
        ),
        ("SELECT ON shop.* WITH REPLACE OPTION", "only WITH GRANT OPTION is allowed"),
        ("SELECT ON shop.* WITH ADMIN OPTION", "only WITH GRANT OPTION is allowed"),
        ("SELECT shop.*", "expected `<privileges> ON <target> [WITH GRANT OPTION]`"),
        ("SELECT ON shop.* WITH GRANT", "expected `<privileges> ON <target> [WITH GRANT OPTION]`"),
        (
            "SELECT(id, name) ON shop.events",
            "column grants, such as SELECT(id), aren't supported in v1",
        ),
        ("SELECT, , INSERT ON shop.*", "a privilege in the list is empty"),
        (
            "FOO; DROP ROLE x ON *.*",
            "`FOO; DROP ROLE x` isn't a privilege name: use letters, digits, underscores and spaces",
        ),
        (
            "SELECT' ON shop.*",
            "`SELECT'` isn't a privilege name: use letters, digits, underscores and spaces",
        ),
    ],
)
def test_syntax_errors_say_why(text, reason):
    with pytest.raises(GrantSyntaxError) as error:
        parse_grant(text)
    assert str(error.value) == reason


TARGET_FORMS = "write `*.*`, `<db>.*`, `<db>.<table>` or a source such as `REMOTE`"


@pytest.mark.parametrize(
    "target, reason",
    [
        ("*", f"`*` isn't a target: {TARGET_FORMS}"),
        ("*.events", f"`*.events` isn't a target: {TARGET_FORMS}"),
        ("shop.t*", "prefix targets such as `shop.t*` aren't supported in v1"),
        ("sh*.*", "prefix targets such as `sh*.*` aren't supported in v1"),
        ("shop.ev*nts", "prefix targets such as `shop.ev*nts` aren't supported in v1"),
        (
            "my-db.*",
            "Invalid database name: 'my-db'. "
            "Must start with letter/underscore and contain only alphanumeric/underscore.",
        ),
        (
            "shop.a.b",
            "Invalid table name: 'a.b'. "
            "Must start with letter/underscore and contain only alphanumeric/underscore.",
        ),
    ],
)
def test_targets_are_star_star_db_star_db_table_or_a_source(target, reason):
    with pytest.raises(GrantSyntaxError) as error:
        parse_grant(f"SELECT ON {target}")
    assert str(error.value) == reason


@pytest.mark.parametrize(
    "written, canonical",
    [
        ("DELETE", "ALTER DELETE"),
        ("update", "ALTER UPDATE"),
        ("SYNC REPLICA", "SYSTEM SYNC REPLICA"),
        ("all  privileges", "ALL"),
        ("select", "SELECT"),
        ("dictget", "dictGet"),
    ],
)
def test_aliases_become_canonical_names(written, canonical):
    checked = check_grant(parse_grant(f"{written} ON shop.*"), TREE)
    assert checked.grant.privileges == (canonical,)
    assert not checked.errors and not checked.warnings


def test_an_unknown_privilege_is_a_warning_and_kept_as_written():
    checked = check_grant(parse_grant("SELECT, SELEKT ON shop.*"), TREE)
    assert checked.grant.privileges == ("SELECT", "SELEKT")
    assert checked.errors == ()
    assert checked.warnings == (
        "ClickHouse 26.3 has no privilege named SELEKT; `access plan` checks it on the server",
    )


@pytest.mark.parametrize(
    "text, error",
    [
        (
            "REMOTE ON *.*",
            "this is the old spelling of a source grant: write `READ, WRITE ON REMOTE`",
        ),
        ("s3 ON *.*", "this is the old spelling of a source grant: write `READ, WRITE ON S3`"),
        (
            "SOURCES ON *.*",
            "this is the old spelling of a source grant: write `READ, WRITE ON *.*`",
        ),
        (
            "CLUSTER, REMOTE ON *.*",
            "REMOTE is the old spelling of a source grant: "
            "write `READ, WRITE ON REMOTE` in an entry of its own",
        ),
    ],
)
def test_the_old_source_spelling_is_rejected_naming_the_new_one(text, error):
    assert check_grant(parse_grant(text), TREE).errors == (error,)


@pytest.mark.parametrize(
    "text, privileges, target",
    [
        ("READ ON REMOTE", ("READ",), Target(source="REMOTE")),
        ("read, write on remote", ("READ", "WRITE"), Target(source="REMOTE")),
        ("WRITE ON S3", ("WRITE",), Target(source="S3")),
        ("READ, WRITE ON *.*", ("READ", "WRITE"), Target()),
    ],
)
def test_read_and_write_take_a_source(text, privileges, target):
    checked = check_grant(parse_grant(text), TREE)
    assert (checked.errors, checked.warnings) == ((), ())
    assert (checked.grant.privileges, checked.grant.target) == (privileges, target)


@pytest.mark.parametrize(
    "text, error",
    [
        ("READ ON shop.*", "READ takes a source, as in `READ ON REMOTE`, or `*.*` for all"),
        ("WRITE ON shop.events", "WRITE takes a source, as in `READ ON REMOTE`, or `*.*` for all"),
        (
            "READ, WRITE ON shop.*",
            "READ and WRITE take a source, as in `READ ON REMOTE`, or `*.*` for all",
        ),
        (
            "SELECT ON events",
            "only READ and WRITE take a source as their target; a table is `<db>.<table>`",
        ),
        (
            "READ, SELECT ON S3",
            "only READ and WRITE take a source as their target; a table is `<db>.<table>`",
        ),
    ],
)
def test_sources_and_tables_are_not_mixed_up(text, error):
    assert check_grant(parse_grant(text), TREE).errors == (error,)


def test_an_unknown_source_is_a_warning():
    checked = check_grant(parse_grant("READ ON NOWHERE"), TREE)
    assert checked.errors == ()
    assert checked.warnings == ("the bundled 26.3 list has no source named NOWHERE",)


@pytest.mark.parametrize(
    "text, where",
    [
        ("CLUSTER ON shop.*", "a database (`<db>.*`)"),
        ("CREATE DATABASE ON shop.events", "a table"),
    ],
)
def test_a_privilege_that_cannot_apply_to_its_target_is_a_warning(text, where):
    checked = check_grant(parse_grant(text), TREE)
    privilege = text.split(" ON ")[0]
    assert checked.warnings == (
        f"in ClickHouse 26.3, {privilege} can't be granted on {where}, so the server rejects it",
    )


def test_a_source_name_must_be_an_identifier():
    with pytest.raises(GrantSyntaxError) as error:
        parse_grant("READ ON my-src")
    assert str(error.value) == (
        "Invalid source name: 'my-src'. "
        "Must start with letter/underscore and contain only alphanumeric/underscore."
    )
