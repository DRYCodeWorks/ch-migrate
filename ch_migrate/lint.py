"""Migration linting: static and runtime analysis rules for ch-migrate."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from ch_migrate.alembic_env import has_current_env
from ch_migrate.baseline import baseline_exemptions, normalize_baseline
from ch_migrate.idempotency import classify_idempotency, waiver_reason
from ch_migrate.mv_validate import MVValidationError, validate_mv_migrations
from ch_migrate.rebase import RevisionGraph, build_revision_graph
from ch_migrate.statements import MigrationStatement, migration_statements

GATE_RULES = frozenset(("idempotency", "standalone_set"))


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class Severity(str, Enum):
    ERROR = "error"
    WARN = "warn"
    INFO = "info"
    OFF = "off"


@dataclass
class LintResult:
    rule: str
    message: str
    severity: Severity
    file: str | None = None
    line: int | None = None
    statement: str | None = None


@dataclass
class LintReport:
    results: list[LintResult] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return any(r.severity == Severity.ERROR for r in self.results)

    @property
    def error_count(self) -> int:
        return sum(1 for r in self.results if r.severity == Severity.ERROR)

    @property
    def warning_count(self) -> int:
        return sum(1 for r in self.results if r.severity == Severity.WARN)

    @property
    def info_count(self) -> int:
        return sum(1 for result in self.results if result.severity == Severity.INFO)


@dataclass
class LintConfig:
    """Lint configuration loaded from config.yaml."""

    large_table_threshold: int = 100_000_000
    rules: dict[str, Severity] = field(default_factory=dict)
    mv_validation_cutoff: str | None = None
    gate_baseline: tuple[str, ...] = ()

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> LintConfig:
        lint_section = config.get("lint", {})
        if not lint_section:
            return cls()

        threshold = lint_section.get("large_table_threshold", 100_000_000)
        rules_raw = lint_section.get("rules", {})
        rules = {}
        for name, level in rules_raw.items():
            if name in GATE_RULES and level != Severity.ERROR:
                raise ValueError(
                    f"lint.rules.{name} must remain error; use an in-file waiver "
                    "or a reviewed lint.gate_baseline instead"
                )
            try:
                rules[name] = Severity(level)
            except ValueError:
                pass

        cutoff = lint_section.get("mv_validation_cutoff")

        return cls(
            large_table_threshold=threshold,
            rules=rules,
            mv_validation_cutoff=cutoff,
            gate_baseline=normalize_baseline(lint_section.get("gate_baseline")),
        )


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------


class LintRule(ABC):
    """Base class for lint rules.

    Subclasses implement `check()` which receives migration SQL and context,
    returning a list of LintResult. Each rule has a `name` used for config lookup.
    """

    name: str = ""
    default_severity: Severity = Severity.WARN
    requires_db: bool = False

    def get_severity(self, config: LintConfig) -> Severity:
        if self.name in GATE_RULES:
            return Severity.ERROR
        return config.rules.get(self.name, self.default_severity)

    @abstractmethod
    def check(
        self,
        sql: str,
        *,
        file_path: str | None = None,
        config: LintConfig | None = None,
        client: Any | None = None,
        database: str | None = None,
        graph: RevisionGraph | None = None,
    ) -> list[LintResult]:
        ...


# ---------------------------------------------------------------------------
# ClickHouse reserved words
# ---------------------------------------------------------------------------

# Subset of CH reserved words that commonly collide with column names.
# Full list is version-dependent; these are the most common traps.
_CH_RESERVED_WORDS = frozenset({
    "add", "after", "alias", "all", "alter", "and", "anti", "any", "array",
    "as", "asc", "attach", "between", "both", "by", "case", "cast", "check",
    "cluster", "collate", "column", "comment", "constraint", "create",
    "cross", "cube", "current", "database", "databases", "date", "day",
    "default", "delete", "desc", "describe", "detach", "dictionaries",
    "dictionary", "distinct", "distributed", "drop", "else", "end", "engine",
    "events", "except", "exists", "explain", "expression", "extract", "fetch",
    "final", "first", "flush", "following", "for", "format", "from", "full",
    "function", "global", "granularity", "group", "having", "hour", "if",
    "ilike", "in", "index", "inject", "inner", "insert", "interval", "into",
    "is", "join", "key", "kill", "last", "layout", "leading", "left", "like",
    "limit", "live", "local", "logs", "materialize", "materialized", "max",
    "merges", "min", "minute", "modify", "month", "move", "mutation", "no",
    "not", "null", "nulls", "offset", "on", "optimize", "or", "order",
    "outer", "outfile", "over", "partition", "populate", "preceding",
    "primary", "prewhere", "projection", "quarter", "range", "reload",
    "remove", "rename", "replace", "right", "rollup", "row", "rows",
    "sample", "second", "select", "semi", "set", "settings", "show",
    "source", "start", "stop", "system", "table", "tables", "temporary",
    "test", "then", "ties", "timestamp", "to", "top", "totals", "trailing",
    "trim", "truncate", "type", "unbounded", "union", "update", "use",
    "using", "uuid", "values", "view", "volume", "watch", "week", "when",
    "where", "window", "with", "year",
})


# ---------------------------------------------------------------------------
# Static rules (no DB connection needed)
# ---------------------------------------------------------------------------


class DestructiveChangeRule(LintRule):
    """Flags DROP TABLE and DROP COLUMN statements."""

    name = "destructive_changes"
    default_severity = Severity.WARN

    _RE_DROP_TABLE = re.compile(
        r"\bDROP\s+TABLE\b", re.IGNORECASE
    )
    _RE_DROP_COLUMN = re.compile(
        r"\bDROP\s+COLUMN\b", re.IGNORECASE
    )

    def check(self, sql: str, **kwargs: Any) -> list[LintResult]:
        config = kwargs.get("config") or LintConfig()
        severity = self.get_severity(config)
        if severity == Severity.OFF:
            return []

        results: list[LintResult] = []
        file_path = kwargs.get("file_path")

        for match in self._RE_DROP_TABLE.finditer(sql):
            line = sql[:match.start()].count("\n") + 1
            results.append(LintResult(
                rule=self.name,
                message="DROP TABLE is destructive and irreversible",
                severity=severity,
                file=file_path,
                line=line,
            ))

        for match in self._RE_DROP_COLUMN.finditer(sql):
            line = sql[:match.start()].count("\n") + 1
            results.append(LintResult(
                rule=self.name,
                message="DROP COLUMN is destructive and irreversible",
                severity=severity,
                file=file_path,
                line=line,
            ))

        return results


class IdempotencyRule(LintRule):
    """Require repeat-safe syntax or a visible, reasoned statement waiver."""

    name = "idempotency"
    default_severity = Severity.ERROR

    def check(self, sql: str, **kwargs: Any) -> list[LintResult]:
        reason = waiver_reason(tuple(kwargs.get("comments", ())))
        severity = Severity.ERROR
        if reason == "":
            message = "A waiver needs a reason: ch-migrate: allow-non-idempotent <reason>"
        elif reason is not None:
            severity = Severity.INFO
            message = f"Idempotency waiver: {reason}"
        else:
            check = classify_idempotency(sql)
            if check.status == "ok":
                return []
            message = (
                check.suggestion
                if check.status == "fix"
                else (
                    "Not idempotent by syntax; add an in-file "
                    "ch-migrate: allow-non-idempotent <reason> waiver"
                )
            )
        first_line = sql.strip().splitlines()[0] if sql.strip() else ""
        return [LintResult(self.name, message, severity, kwargs.get("file_path"), 1, first_line)]


class StandaloneSetRule(LintRule):
    """Reject SET on project environments without the session-safe v2 marker."""

    name = "standalone_set"
    default_severity = Severity.ERROR

    def check(self, sql: str, **kwargs: Any) -> list[LintResult]:
        if kwargs.get("session_safe", False) or not re.match(r"^\s*SET\b", sql, re.IGNORECASE):
            return []
        return [
            LintResult(
                self.name,
                "This SET is ignored on this project's connection. Run `ch-migrate upgrade-env`, "
                "or put the setting in a SETTINGS clause on the statement that needs it.",
                Severity.ERROR,
                kwargs.get("file_path"),
                1,
                sql.strip().splitlines()[0],
            )
        ]


class ReservedWordRule(LintRule):
    """Flags column names that are ClickHouse reserved words."""

    name = "reserved_words"
    default_severity = Severity.WARN

    _RE_COLUMN_DEF = re.compile(
        r"^\s+`?(\w+)`?\s+(?:Nullable|UInt|Int|Float|String|Date|Array|Tuple|Map|Bool|Enum)",
        re.IGNORECASE | re.MULTILINE,
    )

    def check(self, sql: str, **kwargs: Any) -> list[LintResult]:
        config = kwargs.get("config") or LintConfig()
        severity = self.get_severity(config)
        if severity == Severity.OFF:
            return []

        results: list[LintResult] = []
        file_path = kwargs.get("file_path")

        for match in self._RE_COLUMN_DEF.finditer(sql):
            col_name = match.group(1)
            if col_name.lower() in _CH_RESERVED_WORDS:
                line = sql[:match.start()].count("\n") + 1
                results.append(LintResult(
                    rule=self.name,
                    message=f"Column '{col_name}' is a ClickHouse reserved word",
                    severity=severity,
                    file=file_path,
                    line=line,
                ))

        return results


class MissingOnClusterRule(LintRule):
    """Flags DDL without {on_cluster} when cluster is configured."""

    name = "missing_on_cluster"
    default_severity = Severity.OFF  # Off by default — only relevant for clustered setups

    _RE_DDL = re.compile(
        r"\b(CREATE|ALTER|DROP)\s+(?:OR\s+REPLACE\s+)?"
        r"(?:TABLE|VIEW|MATERIALIZED\s+VIEW|DICTIONARY)\b",
        re.IGNORECASE,
    )

    def check(self, sql: str, **kwargs: Any) -> list[LintResult]:
        config = kwargs.get("config") or LintConfig()
        severity = self.get_severity(config)
        if severity == Severity.OFF:
            return []

        results: list[LintResult] = []
        file_path = kwargs.get("file_path")

        for match in self._RE_DDL.finditer(sql):
            # Check if ON CLUSTER or {on_cluster} appears nearby
            rest = sql[match.end():match.end() + 200]
            if not re.search(r"(?:ON\s+CLUSTER|{on_cluster})", rest, re.IGNORECASE):
                line = sql[:match.start()].count("\n") + 1
                stmt_type = match.group(0).strip()
                results.append(LintResult(
                    rule=self.name,
                    message=f"{stmt_type} without ON CLUSTER or {{on_cluster}} placeholder",
                    severity=severity,
                    file=file_path,
                    line=line,
                ))

        return results


# ---------------------------------------------------------------------------
# Runtime rules (require DB connection)
# ---------------------------------------------------------------------------


class LargeTableMutationRule(LintRule):
    """Flags ALTER on tables above a configurable row threshold."""

    name = "large_table_mutation"
    default_severity = Severity.WARN
    requires_db = True

    _RE_ALTER_TABLE = re.compile(
        r"\bALTER\s+TABLE\s+(?:`?(\w+|\{[^}]+\})`?\.)?`?(\w+)`?",
        re.IGNORECASE,
    )

    def check(self, sql: str, **kwargs: Any) -> list[LintResult]:
        config = kwargs.get("config") or LintConfig()
        severity = self.get_severity(config)
        if severity == Severity.OFF:
            return []

        client = kwargs.get("client")
        database = kwargs.get("database")
        if not client or not database:
            return []

        results: list[LintResult] = []
        file_path = kwargs.get("file_path")
        threshold = config.large_table_threshold

        for match in self._RE_ALTER_TABLE.finditer(sql):
            db = match.group(1)
            if db is None or db.startswith("{"):
                db = database
            table_name = match.group(2)
            try:
                result = client.query(
                    "SELECT count() FROM system.parts "
                    "WHERE database = {db:String} AND table = {tbl:String} AND active",
                    parameters={"db": db, "tbl": table_name},
                )
                if result.result_rows:
                    row_count = result.result_rows[0][0]
                    if row_count > threshold:
                        line = sql[:match.start()].count("\n") + 1
                        results.append(LintResult(
                            rule=self.name,
                            message=(
                                f"ALTER on '{table_name}' which has {row_count:,} parts "
                                f"(threshold: {threshold:,})"
                            ),
                            severity=severity,
                            file=file_path,
                            line=line,
                        ))
            except Exception:
                pass

        return results


class MVDependencyRule(LintRule):
    """Flags operations on tables that have materialized view dependencies."""

    name = "mv_dependency"
    default_severity = Severity.WARN
    requires_db = True

    _RE_DROP_TABLE = re.compile(
        r"\bDROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:`?(\w+|\{[^}]+\})`?\.)?`?(\w+)`?",
        re.IGNORECASE,
    )
    _RE_ALTER_TABLE = re.compile(
        r"\bALTER\s+TABLE\s+(?:`?(\w+|\{[^}]+\})`?\.)?`?(\w+)`?",
        re.IGNORECASE,
    )

    def check(self, sql: str, **kwargs: Any) -> list[LintResult]:
        config = kwargs.get("config") or LintConfig()
        severity = self.get_severity(config)
        if severity == Severity.OFF:
            return []

        client = kwargs.get("client")
        database = kwargs.get("database")
        if not client or not database:
            return []

        results: list[LintResult] = []
        file_path = kwargs.get("file_path")

        from ch_migrate.introspect import get_dependencies

        try:
            dep_graph = get_dependencies(client, database)
        except Exception:
            return []

        tables_to_check: list[tuple[str, re.Match[str]]] = []
        for match in self._RE_DROP_TABLE.finditer(sql):
            tables_to_check.append((match.group(2), match))
        for match in self._RE_ALTER_TABLE.finditer(sql):
            tables_to_check.append((match.group(2), match))

        for table_name, match in tables_to_check:
            affected = dep_graph.affected_by_drop(table_name)
            if affected:
                mv_names = [
                    n.name for n in affected if n.obj_type == "materialized_view"
                ]
                dict_names = [
                    n.name for n in affected if n.obj_type == "dictionary"
                ]
                if mv_names or dict_names:
                    line = sql[:match.start()].count("\n") + 1
                    deps = []
                    if mv_names:
                        deps.append(f"MVs: {', '.join(mv_names)}")
                    if dict_names:
                        deps.append(f"Dicts: {', '.join(dict_names)}")
                    results.append(LintResult(
                        rule=self.name,
                        message=(
                            f"'{table_name}' has dependent objects: {'; '.join(deps)}"
                        ),
                        severity=severity,
                        file=file_path,
                        line=line,
                    ))

        return results


class MVDeclarationRule(LintRule):
    """Flags CREATE MATERIALIZED VIEW without MV_DECLARATIONS or required grants.

    When a migration creates a materialized view, ClickHouse requires the
    inserting user to have INSERT on the target table. This rule enforces that
    the migration declares its MV dependencies via MV_DECLARATIONS and that
    companion grants exist in the migration batch.

    Configurable via config.yaml:
        lint:
          rules:
            mv_declarations: error  # error (default), warn, or off
          mv_validation_cutoff: "2026-03-25"  # Optional grandfathering date
    """

    name = "mv_declarations"
    default_severity = Severity.ERROR

    def check(self, sql: str, **kwargs: Any) -> list[LintResult]:
        config = kwargs.get("config") or LintConfig()
        severity = self.get_severity(config)
        graph: RevisionGraph | None = kwargs.get("graph")
        if severity == Severity.OFF or graph is None or not graph.migrations:
            return []
        versions_dir = next(iter(graph.migrations.values())).path.parent
        statements = kwargs.get("statements", {})
        errors = validate_mv_migrations(versions_dir, cutoff_date=config.mv_validation_cutoff)
        results = []
        for error in errors:
            origin = _mv_origin(error, statements.get(error.file, []))
            if origin is not None:
                results.append(
                    LintResult(self.name, error.message, severity, origin.source, origin.line)
                )
        return results


# ---------------------------------------------------------------------------
# Rule registry
# ---------------------------------------------------------------------------

STATIC_RULES: list[LintRule] = [
    DestructiveChangeRule(),
    IdempotencyRule(),
    StandaloneSetRule(),
    ReservedWordRule(),
    MissingOnClusterRule(),
    MVDeclarationRule(),
]

RUNTIME_RULES: list[LintRule] = [
    LargeTableMutationRule(),
    MVDependencyRule(),
]

ALL_RULES: list[LintRule] = STATIC_RULES + RUNTIME_RULES


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def lint_migrations(
    versions_dir: Path,
    *,
    config: LintConfig | None = None,
    client: Any | None = None,
    database: str | None = None,
    revisions: set[str] | None = None,
) -> LintReport:
    """Lint upgrade statements, optionally restricted to an explicit revision set."""
    config = config or LintConfig()
    graph = build_revision_graph(versions_dir)
    scope = _LintScope(
        config, client, database, graph, has_current_env(versions_dir.parent / "env.py")
    )
    exempt = baseline_exemptions(graph, config.gate_baseline)
    selected = {}
    for migration in graph.migrations.values():
        if migration.revision not in exempt and (
            revisions is None or migration.revision in revisions
        ):
            selected[migration.path.name] = [
                statement
                for statement in migration_statements(migration.path)
                if statement.direction == "upgrade"
            ]
    report = LintReport(results=_gate_configuration_errors(config))
    rules = list(STATIC_RULES) + (RUNTIME_RULES if client is not None else [])
    for statements in selected.values():
        for statement in statements:
            report.results.extend(_lint_statement(statement, rules, scope))
    # Declaration checks need the whole grant batch, but report only selected upgrades.
    report.results.extend(
        MVDeclarationRule().check("", config=config, graph=graph, statements=selected)
    )
    return report


@dataclass(frozen=True)
class _LintScope:
    config: LintConfig
    client: Any
    database: str | None
    graph: RevisionGraph
    session_safe: bool


def _gate_configuration_errors(config: LintConfig) -> list[LintResult]:
    return [
        LintResult(
            name,
            f"lint.rules.{name} must remain error; use an in-file waiver "
            "or a reviewed lint.gate_baseline instead",
            Severity.ERROR,
            "config.yaml",
        )
        for name in GATE_RULES
        if name in config.rules and config.rules[name] != Severity.ERROR
    ]


def _lint_statement(
    statement: MigrationStatement, rules: list[LintRule], scope: _LintScope
) -> list[LintResult]:
    results = []
    for rule in rules:
        if isinstance(rule, MVDeclarationRule) or rule.get_severity(scope.config) == Severity.OFF:
            continue
        findings = rule.check(
            statement.sql,
            file_path=statement.source,
            config=scope.config,
            client=scope.client,
            database=scope.database,
            graph=scope.graph,
            comments=statement.comments,
            session_safe=scope.session_safe,
        )
        for finding in findings:
            finding.file = statement.source
            finding.line = statement.line
            finding.statement = statement.sql.splitlines()[0]
        results.extend(findings)
    return results


def _mv_origin(
    error: MVValidationError, statements: list[MigrationStatement]
) -> MigrationStatement | None:
    candidates = []
    for statement in statements:
        match = re.search(
            r"\bCREATE\s+MATERIALIZED\s+VIEW\s+(?:IF\s+NOT\s+EXISTS\s+)?"
            r"(?:(?:\{[^}]*\}|`[^`]+`|\w+)\.)?`?(\w+)`?",
            statement.sql,
            re.IGNORECASE,
        )
        if match:
            candidates.append(statement)
            if error.mv_name is None or match.group(1) == error.mv_name:
                return statement
    return candidates[0] if candidates else None
