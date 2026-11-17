"""What `access.yaml`, the built-in profiles and legacy mode declare, once validated.

Every name here is rendered and valid: placeholders such as `{db}` are filled in, and each
name passed `validate_identifier`. Privilege names in `access.yaml` are canonical; the
built-in profiles keep 0.8's spelling, which the server reads the same way.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Target:
    """Where a grant applies: `*.*`, `db.*`, `db.table`, or a data source such as `REMOTE`."""

    database: str | None = None
    table: str | None = None
    source: str | None = None

    def __str__(self) -> str:
        if self.source is not None:
            return self.source
        return f"{self.database or '*'}.{self.table or '*'}"

    @property
    def scope(self) -> str:
        """`global`, `database`, `table` or `source`: which privilege levels can apply."""
        if self.source is not None:
            return "source"
        if self.database is None:
            return "global"
        return "database" if self.table is None else "table"


@dataclass(frozen=True)
class Grant:
    """`<privileges> ON <target> [WITH GRANT OPTION]`, required or best-effort."""

    privileges: tuple[str, ...]
    target: Target
    grant_option: bool = False
    # Best-effort: the admin gives what it can, as `GRANT CURRENT GRANTS(...)` or, ON CLUSTER,
    # one plain GRANT per privilege, and the rest is a warning (2.4; Decision 12).
    best_effort: bool = False

    def __str__(self) -> str:
        option = " WITH GRANT OPTION" if self.grant_option else ""
        return f"{', '.join(self.privileges)} ON {self.target}{option}"


@dataclass(frozen=True)
class Secret:
    """`{secret: NAME}`: the SSM path at `environments.<env>.ssm.NAME`, else `CH_<ENV>_<NAME>`."""

    name: str


@dataclass(frozen=True)
class Role:
    """A role and the roles and grants it holds."""

    name: str
    grants: tuple[Grant, ...] = ()
    roles: tuple[str, ...] = ()
    absent: bool = False


@dataclass(frozen=True)
class User:
    """A user. `default_roles` None means every role it holds is a default role."""

    name: str
    password: Secret | None = None
    grants: tuple[Grant, ...] = ()
    roles: tuple[str, ...] = ()
    default_roles: tuple[str, ...] | None = None
    absent: bool = False


@dataclass(frozen=True)
class Profile:
    """A built-in grant set for the migration role: `core` or `legacy` (2.4)."""

    name: str
    grants: tuple[Grant, ...]


@dataclass(frozen=True)
class MigrationIdentity:
    """The migration role and user. `bootstrap` and `apply` only ever add to them (2.2)."""

    role: str
    user: str
    profile: Profile
    grants: tuple[Grant, ...] = ()  # `migration_role.grants`, beyond the profile
    password: Secret = Secret("migration_password")

    @property
    def all_grants(self) -> tuple[Grant, ...]:
        return self.profile.grants + self.grants


@dataclass(frozen=True)
class AccessSpec:
    """Everything one environment declares.

    `legacy` is True when the project has no `access.yaml`: the migration role holds the
    `legacy` profile, and the read-only and dictionary roles and users that `config.yaml`
    names are kept additively, as 0.8 kept them (2.4).
    """

    environment: str
    migration: MigrationIdentity
    roles: tuple[Role, ...] = ()
    users: tuple[User, ...] = ()
    legacy: bool = False
