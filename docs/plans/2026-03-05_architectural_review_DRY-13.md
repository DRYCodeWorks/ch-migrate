# Architectural Review: ch-migrate Feature Proposals

**Reviewer:** Principal Architect
**Date:** 2026-03-05
**Design under review:** DRY-11 feature proposals (12 proposed features for ch-migrate)
**Related issues:** [DRY-11](/issues/DRY-11) (feature proposals), [DRY-12](/issues/DRY-12) (design), [DRY-13](/issues/DRY-13) (this review)

---

## Summary

The feature proposal identifies the right market gap: no tool today handles the full ClickHouse lifecycle in a single Python package. The competitive analysis is accurate and the 12 proposed features are individually reasonable. However, the proposal treats ch-migrate as a monolithic CLI where features are bolted on independently. Several features (diff, snapshot, dependency graph, linting) share a common need to introspect a live ClickHouse database -- yet the proposal designs each as a standalone command. This review recommends introducing an **introspection layer** as a foundational module before building analysis features on top, and reorders priorities accordingly. We also flag architectural constraints in the current codebase that the proposal doesn't address.

---

## Key Recommendations

### 1. [Blocker] Introduce an introspection module before building analysis features

**Concern:** Features 1 (diff), 2 (dependency graph), 3 (linting, runtime checks), and 6 (snapshot) all need to query `system.tables`, `system.columns`, `SHOW CREATE TABLE`, and `system.dictionaries`. Without a shared introspection layer, each feature will independently implement system table queries, DDL parsing, and object model construction.

**Recommendation:** Create `clickhouse_alembic/introspect.py` with:
- `get_live_schema(client, database) -> Schema` -- query system tables, return structured object model
- `parse_create_statement(ddl: str) -> ObjectDefinition` -- normalize ClickHouse DDL for comparison
- `get_dependencies(client, database) -> DependencyGraph` -- query `system.tables` for MV source/target relationships

This module becomes the foundation for diff, snapshot, lint, and dependency features. Without it, we build the same plumbing four times.

**Effort:** Medium (but it's amortized across 4 features)

### 2. [Blocker] Address the Alembic subprocess boundary before adding hooks

**Concern:** The current `_run_alembic()` in `cli.py:37-92` delegates to Alembic via subprocess. This is a deliberate design choice (isolation, pyenv compat) but it means ch-migrate cannot intercept individual migration execution. Features 3 (lint pre-checks) and 11 (execution hooks) need to run logic before/after each migration, not just before/after the entire `alembic upgrade` command.

**Recommendation:** For linting, static analysis of SQL files can work without touching the Alembic boundary. But runtime checks (row counts, MV dependency validation) and hooks require either:
- **Option A:** Add an Alembic plugin (`EnvironmentContext.configure` hooks in `env.py`) that calls back into ch-migrate
- **Option B:** Replace subprocess orchestration with in-process Alembic API calls for commands that need hooks

Option A is simpler and preserves the subprocess isolation for `up`/`down`. We'd extend the generated `env.py` to import and call a hook registry.

### 3. [Concern] ON CLUSTER support needs a guard list, not blind appending

**Concern:** The proposal suggests adding `ON CLUSTER {cluster}` config and automatically appending it to DDL. Not all DDL supports ON CLUSTER equally -- dictionary creation, some ALTER operations, and system queries have version-dependent ON CLUSTER support. Blindly appending will produce invalid SQL on older ClickHouse versions.

**Recommendation:** Implement as a template variable `{on_cluster}` that resolves to `ON CLUSTER {cluster_name}` or empty string. Migration authors use it explicitly in their SQL:
```sql
CREATE TABLE {db}.users {on_cluster} (...)
```
This is opt-in per statement rather than automatic, avoiding silent breakage.

### 4. [Concern] PG-to-CH helper should be a separate package, not in ch-migrate core

**Concern:** Feature 8 (Postgres-to-ClickHouse migration helper) introduces `psycopg2` as a runtime dependency, Postgres schema introspection, type mapping tables, and ENGINE/ORDER BY heuristics. This is a substantial body of code with a different concern (ETL/migration) than schema versioning.

**Recommendation:** Build as `ch-migrate-from-pg` -- a separate package that depends on ch-migrate for output (generating migration files) but keeps Postgres concerns isolated. This avoids bloating ch-migrate's dependency surface and allows independent release cycles.

### 5. [Suggestion] Reorder priorities around the introspection layer

**Current proposed order:** EXCHANGE scaffolding -> Linting -> GitHub Actions -> Snapshot -> Diff -> Dependencies -> ON CLUSTER -> PG helper

**Recommended order:**

| Phase | Feature | Rationale |
|-------|---------|-----------|
| 1a | EXCHANGE TABLES scaffolding | Quick win, no new architecture needed |
| 1b | ON CLUSTER template variable | Config change + template variable, small scope |
| 2 | Introspection module | Foundation for 3 subsequent features |
| 3a | Schema snapshot | First consumer of introspection, validates the layer |
| 3b | Schema diff | Second consumer, builds on snapshot |
| 3c | MV dependency graph | Third consumer, highest differentiation |
| 4 | Migration linting (static) | File-based analysis, no DB connection needed |
| 5 | Execution hooks | Requires env.py refactoring |
| 6 | GitHub Actions | Packaging concern, parallel track |
| Separate | PG-to-CH helper | Separate package |

---

## Detailed Findings

### Feature 1: Schema Diff / Drift Detection

**Severity: Concern**

The proposal shows clean CLI output but underestimates the DDL normalization problem. ClickHouse's `SHOW CREATE TABLE` output includes:
- Codec declarations (`CODEC(LZ4HC(9))`)
- TTL expressions with complex date arithmetic
- MATERIALIZED/ALIAS column expressions
- Settings clauses (`SETTINGS index_granularity = 8192`)
- Engine-specific parameters that vary by version

Comparing raw DDL strings will produce false positives. We need a structured comparison that normalizes whitespace, ordering of columns, and default settings that ClickHouse adds implicitly.

**Recommendation:** The introspection module should parse DDL into a structured `TableDefinition` (columns, engine, order_by, partition_by, ttl, settings) and compare field-by-field. String-level diff is a fallback for objects we can't parse, not the primary comparison.

### Feature 2: MV Dependency Graph

**Severity: Suggestion**

Good feature, genuine differentiator. Two implementation notes:

1. **Source of truth is `system.tables`**, not migration files. MVs created outside ch-migrate (manually, by CDC tools) still need to appear in the dependency graph. The proposal implies parsing migration SQL, but live DB introspection is more reliable.

2. **ClickHouse MVs are insert triggers, not views.** Dropping the source table doesn't break the MV definition -- it breaks data flow. The dependency graph should distinguish between "schema dependency" (MV references table in SELECT) and "data flow dependency" (MV triggers on INSERT to source). The proposal conflates these.

### Feature 3: Migration Linting

**Severity: Suggestion**

The proposal mixes static analysis (can run without DB connection) and runtime checks (needs row counts, MV state). These should be two modes:

- `ch-migrate lint` -- static analysis of SQL files (no DB needed, fast, CI-friendly)
- `ch-migrate lint dev` -- static + runtime analysis with live DB (needs connection)

Static rules: missing IF EXISTS, reserved words, destructive DDL detection
Runtime rules: large table mutation warnings, MV dependency checks, ON CLUSTER validation

This separation matters for CI/CD: static linting runs on every PR without credentials.

### Feature 4: EXCHANGE TABLES Scaffolding

**Severity: Note -- on track**

This is the right first feature. It fits naturally into the existing `_create_sql_file()` pattern at `cli.py:401-429`. Implementation is straightforward: add `--exchange` flag to `ch-migrate new` that generates a multi-step SQL template instead of an empty file. No new architecture needed.

One addition: the scaffold should include the `SYSTEM RELOAD DICTIONARY` statement when the table being exchanged is a dictionary source. The current `create_dictionary()` helper in `helpers.py:65-106` already parses source tables -- reuse that logic.

### Feature 5: GitHub Actions

**Severity: Note**

This is packaging and CI, not a code feature. It can proceed in parallel on a separate track without touching ch-migrate's architecture. The action itself is a thin wrapper around `ch-migrate bootstrap` + `ch-migrate up` + `ch-migrate lint`.

Worth noting: the action should use ClickHouse's official Docker image (`clickhouse/clickhouse-server`) and the test should validate both Cloud-compatible (SharedMergeTree) and self-hosted (ReplicatedMergeTree) modes.

### Feature 6: Schema Snapshot

**Severity: Suggestion**

Good feature, natural complement to diff. But the proposal doesn't address:

1. **Objects created outside ch-migrate** (PeerDB tables, manual DDL, system objects). Snapshot should have a `--filter` or `--exclude` option.

2. **Snapshot format.** The proposal puts snapshots in `migrations/sql/snapshot/`. This conflicts with the existing object-centric history structure. Snapshots are point-in-time state, not migration history. Recommend `migrations/sql/snapshots/<timestamp>/` to avoid confusion.

### Feature 7: ON CLUSTER Support

See Key Recommendation #3 above. Template variable approach over automatic appending.

### Feature 8: PG-to-CH Migration Helper

See Key Recommendation #4 above. Separate package.

### Features 9-12 (Declarative Mode, TTL Management, Hooks, TUI)

These are correctly categorized as future/nice-to-have. Declarative mode (feature 9) is a very large scope change that would require rethinking the Alembic foundation. Only pursue if there's strong user demand.

Hooks (feature 11) is the most likely to be needed soon -- dictionary reloads after migrations are a common pattern in a client codebase.

---

## Cross-Cutting Concerns

### Scalability

The current codebase (2K lines, 10 modules) is well-structured for a single-developer tool. Adding 6-8 features could triple the codebase to 6-8K lines. The proposed features cluster into natural modules:

| Module | Features | Estimated size |
|--------|----------|---------------|
| `introspect.py` | Shared by diff, snapshot, deps | ~400 lines |
| `diff.py` | Diff + snapshot | ~300 lines |
| `deps.py` | Dependency graph | ~200 lines |
| `lint.py` | Static + runtime linting | ~300 lines |
| `scaffold.py` | EXCHANGE TABLES templates | ~150 lines |

This keeps each module focused and under 500 lines -- consistent with the current architecture style.

### Operational Burden

The introspection-based features (diff, snapshot, deps) add a new failure mode: stale or incomplete system table data. ClickHouse's `system.tables` is eventually consistent in clustered deployments. The introspection module should document this and add a `--node` flag for targeting specific cluster nodes if needed.

### Security

No new security concerns. The introspection module uses the existing `migration_user` connection which already has `SELECT` on system tables (granted in `bootstrap.py`). No new credentials or elevated permissions needed.

### Incremental Deliverability

The phased approach (EXCHANGE scaffolding -> introspection -> analysis features) allows shipping value at each phase. EXCHANGE scaffolding and ON CLUSTER support ship standalone. Introspection enables the next three features without being user-visible itself.

### Continuity with Existing Architecture

The proposed features follow existing patterns:
- CLI commands via Click (consistent with `cli.py`)
- Config-driven behavior via `config.yaml` (consistent with `config.py`)
- SQL file generation (consistent with `_create_sql_file()`)
- Rich formatted output (consistent with `display.py`)

The one pattern break is the introspection module, which introduces live DB introspection as a first-class concept. Currently, only `status` and `history` read from the DB, and they only touch `alembic_version`. The introspection module queries system tables, which is a new category of DB interaction.

---

## Positive Patterns

1. **Competitive analysis is thorough and accurate.** The gap identification (no Python tool does schema diffing + MV dependency tracking) is correct and well-argued.

2. **EXCHANGE TABLES scaffolding as first feature is the right call.** Quick win, teaches best practices through tooling, builds on existing patterns.

3. **The priority split into tiers is sensible.** Tier 1 features are genuinely high-impact. Tier 3 features are correctly deferred.

4. **The "lead magnet for consulting agency" framing** is a good product lens. Features 1 (diff), 3 (lint), and 8 (PG helper) have the strongest lead magnet potential.

5. **The proposal acknowledges that declarative mode is too large** for the current phase. Good restraint.

---

## Open Questions

1. **[OPEN] Test strategy:** The proposal doesn't mention how to test introspection and diffing features. Do we spin up a ClickHouse container in CI, or mock system tables? This affects the GitHub Actions feature too.

2. **[OPEN] Plugin architecture:** Should ch-migrate support third-party plugins (e.g., community linting rules, custom scaffold templates)? The proposal hints at extensibility but doesn't commit. A plugin interface adds complexity but enables community contributions.

3. **[OPEN] Versioning and compatibility:** Features like diff and snapshot need to handle ClickHouse version differences (e.g., SharedMergeTree only exists in Cloud). Should the introspection module detect CH version and adapt, or should users declare their target version in config?

4. **[OPEN] Existing users:** How do existing ch-migrate users adopt new features? Is there a migration path for projects that already have SQL history files but no snapshot baseline?
