# Changelog

Changes are recorded in [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format.

## [0.5.0]

0.5.x is the last release line that supports Python 3.9. 0.6 requires Python 3.10 or later, because it moves to the official clickhouse-connect dialect.

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

### Fixed

- `bootstrap` on ClickHouse Cloud: the user-management grant now uses `CURRENT GRANTS`, because Cloud's admin cannot grant `ALTER USER`/`DROP USER` on its managed `sql-console` user.
- `new --exchange` keeps Replicated/Shared engine macros such as `{uuid}` and `{replica}` in the scaffolded SQL. Previously `up` failed with `KeyError: 'uuid'` on ClickHouse Cloud and on self-hosted ReplicatedMergeTree tables.
- `ch-migrate new` works on a fresh install. Projects created by `init` no longer run `black` as an Alembic post-write hook; `black` isn't a dependency, so `new` failed with `Could not find entrypoint console_scripts.black`. Existing projects keep their `alembic.ini`; remove the `[post_write_hooks]` section if you don't have `black` installed.
- `status` and `history` after `bootstrap` but before the first `up` show every migration as pending. Previously they reported the database as unreachable, because the version table didn't exist yet.

### Known issues

- If the version-table update is delayed and `up` is run again before it applies, a migration can run twice. Fixed in 0.6; see Q2 in [the dialect spike](docs/design/2026-10-02-dialect-spike.md#q2-the-version-table).
