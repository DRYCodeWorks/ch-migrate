"""The SQL that creates what a declaration holds, for `access validate --sql` and
`bootstrap --dry-run`.

A DBA can review it and run it where ch-migrate has no admin login. It follows `apply`'s
order (2.5): roles, users, memberships, grants, default roles, then drops. It has no
revokes, because offline nothing says what the server holds. A best-effort grant is a
`GRANT CURRENT GRANTS(...)`.

Each password is an unquoted placeholder naming its secret, which is never read, so the SQL
fails to parse until a DBA fills it in. A declared user with no password is never created:
on 26.3 a `CREATE USER` with no `IDENTIFIED` clause is a `no_password` login anyone can use.
Its memberships, grants and default roles are still printed, for a user that must already
exist.

`build_bootstrap_sql`, kept from 0.8, renders legacy mode here too, with each password
written as a quoted string instead of a placeholder.
"""

from __future__ import annotations

from typing import Callable

from ch_migrate.access.model import AccessSpec, Grant, Role, Secret, User
from ch_migrate.access.templates import statement

# What a password is written as, from the secret it is read from.
PasswordText = Callable[[Secret], str]


def render_bootstrap_sql(
    spec: AccessSpec, database: str, password: PasswordText | None = None
) -> list[str]:
    """What `bootstrap` makes: the database first, then everything `spec` declares."""
    return [_sql("create_database", name=database), *render_sql(spec, password)]


def render_sql(spec: AccessSpec, password: PasswordText | None = None) -> list[str]:
    """Every statement, without its trailing semicolon, in the order to run them.

    Each password is `password(secret)`; by default, the placeholder naming the secret.
    """
    roles = [role for role in spec.roles if not role.absent]
    users = [user for user in spec.users if not user.absent]
    holders: list[Role | User] = [*roles, *users]
    return [
        _sql("create_role", name=spec.migration.role),
        *(_sql("create_role", name=role.name) for role in roles),
        *_create_users(spec, users, password or password_placeholder),
        *_memberships(spec, holders),
        *_grants(spec, holders),
        *_default_roles(users),
        *(_sql("drop_user", name=user.name) for user in spec.users if user.absent),
        *(_sql("drop_role", name=role.name) for role in spec.roles if role.absent),
    ]


def users_that_must_exist(spec: AccessSpec) -> list[str]:
    """Declared users with no password: `--sql` doesn't create them, so they must exist."""
    return [user.name for user in spec.users if not user.absent and user.password is None]


def password_placeholder(secret: Secret) -> str:
    """What `--sql` prints, unquoted, in place of a password: it names the secret to fill in."""
    return f"<secret:{secret.name}>"


def _create_users(spec: AccessSpec, users: list[User], password: PasswordText) -> list[str]:
    migration = spec.migration
    passwords = [(migration.user, migration.password), *((u.name, u.password) for u in users)]
    return [
        _sql("create_user", name=name, password=password(secret))
        for name, secret in passwords
        if secret is not None
    ]


def _memberships(spec: AccessSpec, holders: list[Role | User]) -> list[str]:
    statements = [_sql("grant_roles", roles=spec.migration.role, grantee=spec.migration.user)]
    for holder in holders:
        if holder.roles:
            statements.append(
                _sql("grant_roles", roles=", ".join(holder.roles), grantee=holder.name)
            )
    return statements


def _grants(spec: AccessSpec, holders: list[Role | User]) -> list[str]:
    statements = [_grant(grant, spec.migration.role) for grant in spec.migration.all_grants]
    for holder in holders:
        statements += [_grant(grant, holder.name) for grant in holder.grants]
    return statements


def _default_roles(users: list[User]) -> list[str]:
    statements = []
    for user in users:
        if user.default_roles:
            statements.append(
                _sql("set_default_roles", roles=", ".join(user.default_roles), user=user.name)
            )
        elif user.default_roles is not None:
            statements.append(_sql("set_no_default_roles", user=user.name))
    return statements


def _grant(grant: Grant, grantee: str) -> str:
    name = "grant_best_effort" if grant.best_effort else "grant"
    if grant.grant_option:
        name += "_with_grant_option"
    privileges = ", ".join(grant.privileges)
    return _sql(name, privileges=privileges, target=str(grant.target), grantee=grantee)


def _sql(template: str, /, **values: str) -> str:
    return statement("render", template, values)
