# Changelog

Changes are recorded in [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format.

## [Unreleased]

### Changed

- `lint ENV` analyzes only pending revisions after the gate baseline; static `lint` checks every revision after it.
- `up` refuses idempotency gate errors before applying migrations; other lint findings are nonblocking warnings.
- `upgrade-env` records current script heads as a comment-preserving `lint.gate_baseline`.

### Added

- Default mutation waiting with durable ownership, per-replica completion, same-session progress, an optional invocation timeout, and fail-closed unknown-outcome recovery. Upgrades now run in the owning CLI process.
- Resumable insert-first version bookkeeping and fresh waiting-journal generations after downgrade, without replaying completed revision writes.
- Versioned `--json` output and published JSON Schemas for status, history, lint, and diff, with machine-readable errors, graph-resolved applied revisions, explicit waiver reasons, and CI-friendly exit codes.
- Server-backed statement classification with live type comparisons and a 50-statement corpus. Every corpus ALTER is checked against real mutation records or the server's in-place-change rejection.
- A SHA-pinned GitHub Actions drift gate that preserves the JSON report on failure; its install, credential, comparison, and exit steps run against the integration fixture for both matching and out-of-band schemas.
- Reviewed idempotency corpus covering DDL, mutations, inserts, and partition operations.
- Required-reason `ch-migrate: allow-non-idempotent` statement waivers, reported as INFO.
- A `standalone_set` gate error for literal upgrade `SET` statements on environments without the v2 session-safety marker. Version-2 environments, `SETTINGS` clauses, and string literals are not flagged.
- An isolated one-shard/two-replica integration harness with embedded Keeper, owned Docker network cleanup, node stop/start, and distributed-DDL timeout/catch-up coverage.
- Deployment-aware version-table engines: replicated state for self-hosted replicated databases and configured clusters, preserving existing tables without automatic conversion.
- Replica read barriers and failed version-mutation checks, plus live status warnings and an offline upgrade advisory for manual conversion.

## [0.5.1]

### Fixed

- `status` exits 0 again when it cannot reach the database, as it did up to 0.4.1, and prints the connection error as a warning. 0.5.0 made it exit 1, which turned CI jobs that run `status` as a non-blocking reporter into failures whenever the database was briefly unreachable. It still exits 1 when the configuration or `migrations/versions/` is missing.
- Upgrade notes: run `ch-migrate upgrade-env` only for an `env.py` you never edited. It replaces the whole file, so a customized `env.py` (connection settings, session pins) loses those changes; change its `clickhouse_alembic` imports to `ch_migrate` by hand instead. The 0.5.0 notes said to run `upgrade-env` unconditionally.

## [0.5.0]

0.5.x is the last release line that supports Python 3.9. 0.6 requires Python 3.10 or later, because it moves to the official clickhouse-connect dialect.

**Renamed.** The PyPI package is now `ch-migrate-cli` (install with `uv tool install ch-migrate-cli`) and the import package is `ch_migrate`. Releases up to 0.4.1 were `clickhouse-alembic` / `clickhouse_alembic`; Alembic is how `ch-migrate` stores revisions, not what it is. The command is still `ch-migrate`. Uninstall `clickhouse-alembic` before installing `ch-migrate-cli`, because both install the `ch-migrate` command. Existing projects keep working: `clickhouse_alembic` remains as an alias that warns where it's imported and is removed in 1.0. To move over, replace `clickhouse_alembic` with `ch_migrate` in your migration files and run `ch-migrate upgrade-env`.

### Added

- SQL-first migrations: `ch-migrate new ENV NAME` writes an upgrade and a downgrade SQL file plus the revision that runs them, with no Python edits. `--table`, `--view` and `--dict` group the files by object; `--python` keeps the Python template.
- `run_sql()` runs a multi-statement SQL file one statement at a time, stops at the first failure, and leaves literal colons, percent signs and comments untouched online and in offline `--sql` output.
- Irreversible migrations: `new --irreversible REASON` writes only an upgrade file and marks the revision. `down` refuses an entire range that crosses a marked revision before running anything. It lists every migration in the range, marks which are irreversible, and names a `-r` target that reverts only the migrations above them. Direct Alembic calls are refused by the `IrreversibleMigration` exception.
- A README that covers every command, with a quick start that needs no Python.

### Changed

- `new --exchange` scaffolds are marked irreversible: their `downgrade()` now raises `IrreversibleMigration` instead of `NotImplementedError`, so `down` refuses the range before running anything.
- Offline (`--sql`) runs render from `base` using the existing version-table engine.
- `lint` checks upgrade statements one at a time and names the SQL file and line of each finding. It no longer reports findings from downgrade SQL.
- `lint ENV` checks only pending revisions. It fails if it cannot work out which revisions are pending, rather than falling back to checking every local revision. `lint` with no environment still checks every revision.
- One output style across commands. Each line starts with `→` (step), `✓` (done), `!` (warning) or `✗` (error), and next steps name the command to run. Colour is dropped when output is piped or `NO_COLOR` is set, and long lines are no longer wrapped, so paths and SQL stay greppable.
- `up` and `down` print one line per migration instead of Alembic's log lines, then a summary such as "Applied 2 migrations; dev is at head."
- A failed migration reports the migration, the SQL file, the statement number and line, and ClickHouse's error, instead of a Python traceback. `up --verbose` and `down --verbose` add the traceback.
- `new` prints the new revision ID and project-relative paths, without Alembic's absolute "Generating ..." line.
- `lint` prints one finding per line as `file:line  message  [rule]`, instead of a table that wrapped long messages.
- `status` exits 1 when it cannot reach the database, and suggests `ch-migrate up` when migrations are pending. Its panel, and the `snapshot` panel, are sized to their content.
- `snapshot`, `diff` and `deps` leave out Alembic's `alembic_version` table. `diff` also ignores it in snapshots taken by earlier versions.
- `diff` prints one line per drift finding, such as `✗ events (table): column 'country' LowCardinality(String) is in the database but not in the snapshot`, instead of a table. It suggests writing a migration or taking a new snapshot.

### Fixed

- `bootstrap` on ClickHouse Cloud: the user-management grant now uses `CURRENT GRANTS`, because Cloud's admin cannot grant `ALTER USER`/`DROP USER` on its managed `sql-console` user.
- `new --exchange` keeps Replicated/Shared engine macros such as `{uuid}` and `{replica}` in the scaffolded SQL. Previously `up` failed with `KeyError: 'uuid'` on ClickHouse Cloud and on self-hosted ReplicatedMergeTree tables.
- `ch-migrate new` works on a fresh install. Projects created by `init` no longer run `black` as an Alembic post-write hook; `black` isn't a dependency, so `new` failed with `Could not find entrypoint console_scripts.black`. Existing projects keep their `alembic.ini`; remove the `[post_write_hooks]` section if you don't have `black` installed.
- `status` and `history` after `bootstrap` but before the first `up` show every migration as pending. Previously they reported the database as unreachable, because the version table didn't exist yet.

### Known issues

- If the version-table update is delayed and `up` is run again before it applies, a migration can run twice. Fixed in 0.6; see Q2 in [the dialect spike](docs/design/2026-10-02-dialect-spike.md#q2-the-version-table).
