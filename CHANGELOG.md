# Changelog

Changes are recorded in [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format.

## [0.9.0]

0.9 declares users, roles and grants in `access.yaml`. `ch-migrate access plan` shows how a server differs from it, and `access apply` makes the server match, logged in as the admin. A new project's migration role holds only what migrations need, with no right to manage users or roles. A normal `pip install -U` reaches 0.9 from 0.8, so read this before upgrading:

1. **A project without `access.yaml` keeps 0.8's grants, but `bootstrap` reports differently.**
   - It creates the migration role and user before any grant, attempts every grant and names each one that fails. **With an admin that can't give every grant, the role and user now exist and hold every grant the admin could give, and `bootstrap` exits 1 naming each failed grant.** 0.8 stopped at the first refused grant, `CREATE FUNCTION`, before it created the migration user.
   - A best-effort `CURRENT GRANTS` that lands short is now a warning instead of passing silently.
   - It ends by checking the migration role's grants, logged in as the migration user. **If it can't log in as that user, for example because the user's password differs from its secret, it exits 1.** To make them match, run `ALTER USER <migration_user> IDENTIFIED BY '<secret>'` as the admin; `access apply --reset-passwords` resets only the users `access.yaml` declares. 0.8 never logged in as the migration user.
   - **It needs ClickHouse 25.8 or later,** with or without an `access.yaml`, and exits 2 before any change on an older server. 0.8's `bootstrap` already failed on 24.8.
2. **An error before any change now exits 2 instead of 1,** such as a missing secret, a `config.yaml` problem or a server that can't be reached. Update CI jobs that test for exit 1.
3. **`init` writes `access.yaml`.** Running `init` again in an existing project adds one, and the next `bootstrap` then gives the `core` profile instead of 0.8's grants. Delete the file to keep 0.8's behavior.
4. **Legacy `bootstrap` still runs on one node.** Without an `access.yaml` it never uses `ON CLUSTER`, even with `cluster` set, and sends best-effort grants as `CURRENT GRANTS`, as 0.8 does. On a self-hosted cluster that keeps users and roles on each node's own disk, bootstrap each node.
5. **To adopt `access.yaml` in an existing project,** set `migration_role.profile: legacy` if your migrations create users or roles or grant to other users, start each existing user or role from `ch-migrate access show ENV NAME`, written to another file and copied in, and review the first `access plan` before `access apply`. `apply` revokes, from each user and role the file declares, every grant the file leaves out. With an `access.yaml`, `create_dictionary` no longer grants to `dict_reader`, even under `profile: legacy`: declare the reader and its `SELECT` there.
6. **No `upgrade-env` this time.**

To stay on 0.8, pin `ch-migrate-cli==0.8.0`.

### Added

- `access.yaml` declares users, roles, the roles each one holds (with default roles) and grants (with grant option), with a block per environment and `absent: true` to drop a user or role. An `access.yaml` that is empty, or only comments, is an error, not legacy mode: delete it to keep 0.8's grants. Grants use ClickHouse's own words. `{db}`, `{project}`, `{env}` and `{migration_role}` are placeholders, and `{secret: NAME}` reads a password from SSM or from `CH_<ENV>_<NAME>`.
- `ch-migrate access validate ENV [--sql]` checks `access.yaml` and the built-in profiles offline, with no server and no secrets. `--sql` prints the SQL for a DBA to review and run, with each password as an unquoted placeholder. A privilege name the bundled ClickHouse 26.3 list doesn't know is a warning, and so is a `READ` or `WRITE` on a source without the other direction.
- `ch-migrate access show ENV [NAME...]` prints what the server holds, in `access.yaml` form: the starting point for adopting existing users and roles.
- `ch-migrate access plan ENV` lists each create, grant, revoke and drop without changing anything. It exits 0 in sync, 1 with changes pending and 2 on an error. `READ` and `WRITE` on one source compare as one grant, because ClickHouse keeps them apart only with `access_control_improvements.enable_read_write_grants` on, which is off by default.
- `ch-migrate access apply ENV [--reset-passwords]` makes the server match, logged in as the admin. It grants before it revokes, and reads every secret before its first change. It narrows a grant's scope with one `GRANT ... WITH REPLACE OPTION`, which keeps what is held inside a declared best-effort grant's scope; a replace that can't keep that exactly, such as `SELECT ON system.*` held less `system.users`, isn't sent: it's skipped and named, the user or role keeps its access, and `apply` exits 1. Revokes, replaces and default-role changes run only if every earlier change succeeded; otherwise they're skipped and named, so old access survives and a role the user keeps stays active at login. A user whose create or password reset fails gets nothing more from that run. A role's replace runs before that of any user or role that holds it, whatever the order in `access.yaml`. Users and roles the file doesn't declare are never touched.
- `ch-migrate access check ENV` checks, logged in as the migration user, that it holds the migration role's grants, for servers where ch-migrate has no admin login.
- Two built-in profiles for the migration role: `core`, what migrations need on the project's database, with no grant option and no user or role management; and `legacy`, 0.8's grants exactly. `migration_role.grants` in `access.yaml` adds to either. `bootstrap` and `apply` only ever add to the migration role.
- `migration_role` in `config.yaml` names a migration role that another project directory shares.
- Where users and roles are replicated, as on ClickHouse Cloud, `access show`, `plan`, `apply` and `bootstrap` read every replica at once, through `clusterAllReplicas` on `cluster` from `config.yaml`, or else on the one cluster that holds the node and another host. A row missing on every replica is absent at once, with no wait. Replicas that disagree are read again until they agree; if they still disagree at the deadline, one line names them, and what the node connected to holds counts. Where every replica can't be read, for example because the admin lacks `READ ON REMOTE` or only one node is listed, a missing row on the connected node is read again before it counts as absent. The fallback reason prints once, only when the command actually waits.
- `access_read_timeout` in `config.yaml`: a deadline, in seconds from the first read, for the access commands and `bootstrap` to keep reading where a replica is behind. Read time counts toward it as well as the waits; one last read may start at the deadline, and each of its requests is bounded by the driver's request timeout. It must be a finite number, 0 or more; `.inf` or `.nan` exits 2 before any change. The default is 30.
- Where the server grants `CLUSTER` to every login, as ClickHouse Cloud does, the best-effort `CLUSTER ON *.*` counts as held: `plan`, `apply`, `check` and `bootstrap` don't warn about it, and `apply` doesn't try it on every run.
- Users and roles are created in the first user directory that can take one, and that is the storage the access commands read: ClickHouse Cloud lists its read-only `jwt` directory first.
- `show`, `plan`, `apply` and `check`, and `bootstrap`, with or without an `access.yaml`, need ClickHouse 25.8 or later, and exit 2 on an older server, naming its version. 0.8's `bootstrap` already failed on 24.8.
- On a self-hosted cluster that keeps users and roles on each node's own disk, with `cluster` set, `apply`, and `bootstrap` with an `access.yaml`, run every statement `ON CLUSTER`, with the cluster's name backquoted so a name such as `c-1` works, and send each best-effort privilege as its own `GRANT ... ON CLUSTER`. There `access plan`, `access apply`, and `bootstrap` with an `access.yaml`, read only the node they connect to, and print a line naming it before any change. An `ON CLUSTER` change that failed on another node goes unseen by the next run against this one, so after a failed `ON CLUSTER` apply, run `access plan` against each node. Reading every node is a planned follow-up.

### Changed

- `bootstrap` runs on the access engine. With an `access.yaml`, it gives the migration role its profile and `migration_role.grants`, and `access apply` makes the rest. Without one, it gives 0.8's grants, as the upgrade notes describe.
- `bootstrap --dry-run` reads no secret and prints each password as a `<secret:NAME>` placeholder, which fails to parse if run unedited. With an `access.yaml` it prints every statement, without `ON CLUSTER`, because offline it can't tell what the server already holds.
- `init` writes `access.yaml`, with the `core` profile and commented examples.
- `create_dictionary` grants nothing in a project with an `access.yaml`: declare the dictionary's reader and its `SELECT` there, and run `access apply` before `up`. Without one, it grants `SELECT` on the source table to `dict_reader` first, as before.

### Deprecated

- `build_bootstrap_sql`. It returns the same statements as 0.8, rendered from the built-in `legacy` profile, with the roles and users created first.

### Removed

- `ch_migrate/templates/bootstrap/init_users.sql`, which no code read.

### Fixed

- `up`'s failure report, `--verbose` traceback included, hides every secret ch-migrate has read, raw, URL-encoded and as SQLAlchemy escapes it. `create_dictionary` writes the reader's password into the statement, and SQLAlchemy's message quotes the statement.
- `create_dictionary` works with a reader password that starts with `:`. SQLAlchemy read it as a bind parameter, so the migration failed and its error named part of the password.
- `create_dictionary` reads the reader's password from the environment's `dict_reader_password` secret, its `ssm:` path in `config.yaml` or `CH_<ENV>_DICT_READER_PASSWORD`, when `DICT_READER_PASSWORD` isn't set. It read only `DICT_READER_PASSWORD`, which neither `config.yaml` nor the `.env.local` template sets.

## [0.8.0]

0.8 gives CI a machine-readable view of a migration project. `status`, `history`, `lint` and `diff` take `--json` and print one versioned document to stdout, and the README has a GitHub Actions job that fails a build when the live schema drifts from a committed snapshot. Without `--json` nothing changes: output and exit codes are the same as 0.7, and there is no `upgrade-env`.

Every document carries `"schema_version": 1`. The format can still change before 1.0.

To stay on 0.7, pin `ch-migrate-cli==0.7.0`.

### Added

- Versioned `--json` output and published JSON Schemas for status, history, lint, and diff, with machine-readable errors, graph-resolved applied revisions, explicit waiver reasons, and CI-friendly exit codes.
- A SHA-pinned GitHub Actions drift gate that preserves the JSON report on failure; its install, credential, comparison, and exit steps run against the integration fixture for both matching and out-of-band schemas.

## [0.7.0]

0.7 waits for the work a migration starts. `up` doesn't record a migration as applied until the mutations and `ON CLUSTER` DDL its statements started have finished, and it shows their progress while it waits. A normal `pip install -U` reaches it from 0.6, so read this before upgrading:

1. **`up` now blocks until that work finishes, with no default timeout.** If a CI job's time limit stops it mid-wait, ClickHouse keeps working, and the next `up` waits for that work before it runs the migration again. `--timeout SECONDS` sets a limit of your own.
2. **A rerun runs the stopped migration again from its first statement, but only on its own when every statement passes the idempotency gate without a waiver.** An interrupted migration with a waived statement, which includes every `UPDATE`, `DELETE` and `INSERT ... SELECT`, stops the next run until you check what the earlier one changed and name it with `--rerun-interrupted REV`. CI jobs that retry `up` after a timeout need that step.
3. **The gate now reads more of your migrations.** SQL in `text("...")` and SQL sent through `op.get_bind().execute(...)` or `exec_driver_sql(...)` is checked, and SQL built at run time needs a waiver. `lint` and `up` can report migrations that passed before; waive them or keep them behind `lint.gate_baseline`.
4. **Two grants, only where `bootstrap` couldn't grant `SELECT ON system.*`**, such as on ClickHouse Cloud. As the admin user, replacing `<project>` with your project name:
   ```sql
   GRANT CURRENT GRANTS(SELECT ON system.parts) TO <project>_migration_role;
   GRANT CURRENT GRANTS(SELECT ON system.replicas) TO <project>_migration_role;
   ```
   `system.parts` gives the progress bar its bytes and time estimate; without it the bar counts parts. `system.replicas` is read only for self-hosted replicated tables, so Cloud needs just the first.
5. **No `upgrade-env` this time.** The environment 0.6 wrote is current. `up` creates one small table, `_ch_migrate_in_progress`, in the project database.

To stay on 0.6, pin `ch-migrate-cli==0.6.0`.

### Changed

- `up` waits before it records a migration as applied. After each statement that can write, it polls `system.mutations` on the migration session until the project database has no unfinished mutation on any replica that has to apply it, instead of holding a request open with `mutations_sync`. Mutations another writer started count too, and ones already queued are waited for before the first migration runs. The version table's own mutations stay with its 0.6 bookkeeping.
- `ON CLUSTER` statements wait inside ClickHouse: `up` sends them with `distributed_ddl_output_mode = 'throw'` and a limit of 270 seconds, or what's left of `--timeout` if that's less, so a host that fails or doesn't finish in time fails the migration with ClickHouse's message.
- A failed mutation fails the migration with ClickHouse's `latest_fail_reason` and prints the scoped `KILL MUTATION` statement for an operator; `ch-migrate` never kills a mutation itself. A failed mutation from before the run blocks it the same way. After a kill, fix the SQL and run `up` again, naming the revision with `--rerun-interrupted` if it's waived.
- `up` runs Alembic in its own process, so killing `up` can't leave a child process advancing the version table. `down` and a direct `alembic upgrade` go through the same environment and wait too; `--timeout` exists only on `ch-migrate up`.
- The idempotency gate checks literal SQL in `text("...")` and in `execute`/`exec_driver_sql` on any connection, requires a waiver for SQL that only exists at run time, and runs from `env.py` for a direct `alembic upgrade`.

### Added

- `up --timeout SECONDS`: one waiting budget for the whole run. When it runs out, `up` exits 1, names what is still pending and leaves the work running; the next `up` waits on it again, then runs the migration.
- Progress while `up` waits: in a terminal, a live bar per mutation weighted by bytes from `system.parts`, with parts and time left, and one row per replica. Piped output gets plain lines when a wait starts, at each 10% or every 30 seconds, and when it finishes. Without access to `system.parts` the bar counts parts only.
- `up --rerun-interrupted REV` and `down --rerun-interrupted REV` (`-x rerun_interrupted=REV` with `alembic`): run an interrupted migration again after checking what its earlier run changed. Each migration is noted in `_ch_migrate_in_progress` while it runs, created with the version table's engine.
- `bootstrap` grants `SELECT` on `system.replicas` and `system.parts`.

## [0.6.0]

0.6 runs migrations on ClickHouse's official clickhouse-connect dialect in one session-safe connection, and stops a migration from running twice. A normal `pip install -U` reaches it from 0.5.x, so read this before upgrading:

1. **Python 3.10 or later.** On Python 3.9, pip keeps installing 0.5.x.
2. **Run `ch-migrate upgrade-env` in each project.** `up`, `down` and `history` refuse the old environment until you do. It replaces the whole `migrations/env.py` and saves the old file as `migrations/env.py.bak`; reapply any customizations from the backup.
3. **Check `ch-migrate lint ENV`.** The idempotency gate can now block `up`, but only for revisions written after the baseline that `upgrade-env` records.

To stay on 0.5.x until then, pin `ch-migrate-cli==0.5.1`.

### Changed

- **Python 3.10 or later is required.** 0.5.x is the last line that supports Python 3.9.
- Migrations run on the official clickhouse-connect dialect (`clickhousedb://`) in one checked HTTP session per run, so a standalone `SET` applies to later statements. Run `ch-migrate upgrade-env` after upgrading; old environments are refused before connecting.
- The migration session uses its own single-connection pool. ClickHouse Cloud routes each TCP connection, not each session id, to a replica, so the session stays on the connection that created it. A request that reaches a replica without the session fails with `SESSION_NOT_FOUND` instead of running with default settings.
- The idempotency gate can block `up` on projects upgrading from 0.5. `upgrade-env` sets the baseline, so only revisions written after it are gated.
- The version table no longer lets an interrupted run re-run a migration. 0.5.x and earlier can, if `up` runs again before a delayed version update applies.
- `lint ENV` analyzes only pending revisions after the gate baseline; static `lint` checks every revision after it.
- `up` refuses idempotency gate errors before applying migrations; other lint findings are nonblocking warnings.
- `upgrade-env` records current script heads as a comment-preserving `lint.gate_baseline`.

### Added

- Reviewed idempotency corpus covering DDL, mutations, inserts, and partition operations.
- Required-reason `ch-migrate: allow-non-idempotent` statement waivers, reported as INFO.
- Gate findings say how to make the flagged statement safe to run twice, using its own names: the exact `IF NOT EXISTS` or `IF EXISTS` rewrite, or what a retry would do and the usual fix, such as `TRUNCATE` before a backfill or `REPLACE PARTITION` instead of `ATTACH PARTITION ... FROM`.
- A `standalone_set` gate error for literal upgrade `SET` statements on environments without the v2 session-safety marker. Version-2 environments, `SETTINGS` clauses, and string literals are not flagged.
- An isolated one-shard/two-replica integration harness with embedded Keeper, owned Docker network cleanup, node stop/start, and distributed-DDL timeout/catch-up coverage.
- Deployment-aware version-table engines: replicated state for self-hosted replicated databases and configured clusters, preserving existing tables without automatic conversion.
- Replica read barriers and failed version-mutation checks, plus live status warnings and an offline upgrade advisory for manual conversion.
- Every version-row INSERT, from `up`, `down` and `stamp`, carries its own deduplication token, so Replicated and Shared MergeTree cannot drop a re-inserted earlier head and leave the version table empty.

### Fixed

- `ch-migrate --version` works. In 0.5.1 it failed with `RuntimeError: 'ch_migrate' is not installed` in PyPI installs as well as editable ones, because click could not map the `ch_migrate` package to the `ch-migrate-cli` distribution.

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
