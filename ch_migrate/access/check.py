"""`access check`: whether the migration user holds the migration role's grants (2.5).

It runs as the migration user, for servers where ch-migrate has no admin login, and sends
one `CHECK GRANT` for each grant of the migration role: its profile plus
`migration_role.grants`. `bootstrap` ends with the same check. The SQL is in `sql/check.sql`.

- **Each grant is checked as the flags it means on its target** (2.7), from the server's own
  `system.privileges`, which any login can read. `CHECK GRANT` on a group checks every flag
  in it, even those its target can't hold: `ACCESS MANAGEMENT ON db.*` answers 0 right after
  that very grant, because the group's user and role rights apply only on `*.*`. A group that
  is only partly held still answers 0, so it counts as missing.
- **No grant option.** `CHECK GRANT` takes none, so only the privileges are checked.
- A required grant that isn't held is missing, and the check fails. A best-effort one is a
  warning.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from ch_migrate.access.live import read_tree
from ch_migrate.access.model import Grant, MigrationIdentity
from ch_migrate.access.privileges import PrivilegeTree
from ch_migrate.access.rights import grant_problems, rights_of
from ch_migrate.access.server import server_errors
from ch_migrate.access.templates import statement
from ch_migrate.access.writes import grant_clause


@dataclass(frozen=True)
class Verdict:
    """One grant as checked, without grant option, and whether the user holds it."""

    grant: Grant
    held: bool
    # Why the server can't check it, such as a privilege it doesn't know.
    problem: str = ""

    def __str__(self) -> str:
        return f"{self.grant}: {self.problem}" if self.problem else str(self.grant)


@dataclass(frozen=True)
class Checked:
    """Every grant of the migration role, checked as the migration user."""

    user: str
    role: str
    verdicts: tuple[Verdict, ...]

    @property
    def exit_code(self) -> int:
        """0 when every required grant is held, else 1 (2.5)."""
        return 1 if self.missing else 0

    @property
    def missing(self) -> tuple[Verdict, ...]:
        """The required grants the user doesn't hold."""
        return tuple(v for v in self.verdicts if not v.held and not v.grant.best_effort)

    @property
    def shortfalls(self) -> tuple[Verdict, ...]:
        """The best-effort grants the user doesn't hold: warnings only (2.4)."""
        return tuple(v for v in self.verdicts if not v.held and v.grant.best_effort)

    @property
    def required(self) -> int:
        return sum(1 for verdict in self.verdicts if not verdict.grant.best_effort)


def check_migration(client: Any, migration: MigrationIdentity) -> Checked:
    """Check each grant of the migration role, logged in as the migration user."""
    with server_errors():
        tree = read_tree(client)
        verdicts = tuple(_verdict(client, tree, grant) for grant in migration.all_grants)
    return Checked(migration.user, migration.role, verdicts)


def _verdict(client: Any, tree: PrivilegeTree, grant: Grant) -> Verdict:
    checked = replace(grant, grant_option=False)
    problems = grant_problems(tree, checked)
    if problems:
        return Verdict(checked, held=False, problem="; ".join(problems))
    flags = tuple(sorted({right.flag for right in rights_of(tree, checked)}))
    sql = statement(
        "check", "check_grant", {"grants": grant_clause(replace(checked, privileges=flags))}
    )
    return Verdict(checked, held=int(client.command(sql)) == 1)
