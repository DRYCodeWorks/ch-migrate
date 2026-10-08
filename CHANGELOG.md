# Changelog

Changes are recorded in [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format.

## [0.5.0]

0.5.x is the last release line that supports Python 3.9. 0.6 requires Python 3.10 or later, because it moves to the official clickhouse-connect dialect.

### Added

- SQL-first migrations: `ch-migrate new ENV NAME` writes an upgrade and a downgrade SQL file plus the revision that runs them, with no Python edits. `--table`, `--view` and `--dict` group the files by object; `--python` keeps the Python template.
- `run_sql()` runs a multi-statement SQL file one statement at a time, stops at the first failure, and leaves literal colons, percent signs and comments untouched online and in offline `--sql` output.
- Irreversible migrations: `new --irreversible REASON` writes only an upgrade file and marks the revision. `down` refuses an entire range that crosses a marked revision before running anything; direct Alembic calls are refused by the `IrreversibleMigration` exception.
- A README that covers every command, with a quick start that needs no Python.

### Changed

- `new --exchange` scaffolds are marked irreversible: their `downgrade()` now raises `IrreversibleMigration` instead of `NotImplementedError`, so `down` refuses the range before running anything.
- Offline (`--sql`) runs render from `base` using the existing version-table engine.

### Fixed

- `bootstrap` on ClickHouse Cloud: the user-management grant now uses `CURRENT GRANTS`, because Cloud's admin cannot grant `ALTER USER`/`DROP USER` on its managed `sql-console` user.
- `new --exchange` keeps Replicated/Shared engine macros such as `{uuid}` and `{replica}` in the scaffolded SQL. Previously `up` failed with `KeyError: 'uuid'` on ClickHouse Cloud and on self-hosted ReplicatedMergeTree tables.
- `ch-migrate new` works on a fresh install. Projects created by `init` no longer run `black` as an Alembic post-write hook; `black` isn't a dependency, so `new` failed with `Could not find entrypoint console_scripts.black`. Existing projects keep their `alembic.ini`; remove the `[post_write_hooks]` section if you don't have `black` installed.

### Known issues

- If the version-table update is delayed and `up` is run again before it applies, a migration can run twice. Fixed in 0.6; see Q2 in [the dialect spike](docs/design/2026-10-02-dialect-spike.md#q2-the-version-table).
