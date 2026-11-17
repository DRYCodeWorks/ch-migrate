# ch-migrate

## What it is

`ch-migrate` manages SQL-first ClickHouse migrations across environments: author SQL files, bootstrap databases and roles, inspect migrations and dependencies, and compare schema snapshots. Alembic owns revision history; ClickHouse's official `clickhouse-connect[alembic]` integration owns the dialect and DDL compilation. ch-migrate supplies the operational layer above them, without implying an endorsement from ClickHouse.

Background: [ClickHouse migrations with Alembic](https://www.drycodeworks.com/articles/dev-guides/clickhouse-migrations-with-alembic).

## Install

```bash
uv tool install ch-migrate-cli
# Or:
pip install ch-migrate-cli
ch-migrate --version
```

The command is `ch-migrate`, the PyPI package is `ch-migrate-cli` (PyPI treats `ch-migrate` as the same name as the existing, unrelated `chmigrate`), and migrations import from `ch_migrate`. Versions up to 0.4.1 were published as `clickhouse-alembic` with the import package `clickhouse_alembic`; that import still works with a deprecation warning until 1.0, so existing migration files keep running. Replace `clickhouse_alembic` with `ch_migrate` in them when convenient. For `migrations/env.py`, run `ch-migrate upgrade-env` only if you never edited it; if you did (for example to add connection settings), change its `clickhouse_alembic` imports to `ch_migrate` by hand instead, because `upgrade-env` replaces the whole file.

To switch an existing install, remove the old package first, because both install the `ch-migrate` command and the `clickhouse_alembic` folder: `uv tool uninstall clickhouse-alembic && uv tool install ch-migrate-cli`, or `pip uninstall clickhouse-alembic && pip install ch-migrate-cli`. In a project that lists `clickhouse-alembic` as a dependency, replace it with `ch-migrate-cli`.

This README describes the source checkout, which may be ahead of PyPI. To try an unreleased checkout locally, run `uv tool install .` in the repository. For development without installing a global tool, use `uv run --locked ch-migrate`.

## Upgrade from 0.x

**From 0.8:** 0.9 needs no `upgrade-env`. It adds `access.yaml` and the `access` commands
(see [Access: users, roles and grants](#access-users-roles-and-grants)). A project without
an `access.yaml` keeps 0.8's grants: `bootstrap` gives the migration role the same grants
as before, on the node it connects to. What changes in `bootstrap`:

- It creates the migration role and user before any grant, attempts every grant and names
  each one that fails. With an admin that can't give every grant, the role and user now
  exist and hold what the admin could give, and `bootstrap` exits 1. 0.8 stopped at the
  first refused grant, before it created the migration user.
- A best-effort `GRANT CURRENT GRANTS(...)` that lands short, such as
  `SELECT ON system.*` from an admin that holds only part of it, is now a warning instead
  of passing silently.
- It ends by logging in as the migration user to check the role's grants. If it can't log
  in, for example because the user's password differs from its secret, it exits 1. To make
  them match, run `ALTER USER <migration_user> IDENTIFIED BY '<secret>'` as the admin:
  `access apply --reset-passwords` resets only the users `access.yaml` declares.
- It needs ClickHouse 25.8 or later, with or without an `access.yaml`, and exits 2 before
  any change on an older server. 0.8's `bootstrap` already failed on 24.8.
- An error before any change, such as a missing secret or an unreachable server, exits 2
  instead of 1. Update CI jobs that test for exit 1.

`init` now writes an `access.yaml`. Running `init` again in an existing project adds one,
and the next `bootstrap` then gives the core profile instead of 0.8's grants. Delete the
file to keep 0.8's behavior, or adopt it as described under
[Access](#access-users-roles-and-grants). A loose requirement such as `ch-migrate-cli`
upgrades to 0.9; pin `ch-migrate-cli==0.8.0` to stay on 0.8.

**From 0.7:** 0.8 needs no `upgrade-env`, and nothing changes unless you pass
`--json` (see [Drift detection in CI](#drift-detection-in-ci)). A loose requirement
such as `ch-migrate-cli` upgrades to 0.8; pin `ch-migrate-cli==0.7.0` to stay on 0.7.

**From 0.6:** 0.7 needs no `upgrade-env`. `up` now waits for the mutations and
`ON CLUSTER` DDL its statements start (see [Migrations that wait](#migrations-that-wait)),
so give CI jobs time to finish or pass `--timeout`. Where `bootstrap` couldn't grant
`SELECT ON system.*`, such as on ClickHouse Cloud, add the grants listed in the
[CHANGELOG](CHANGELOG.md). A loose requirement such as `ch-migrate-cli` upgrades to
0.7; pin `ch-migrate-cli==0.6.0` to stay on 0.6.

**From 0.5:** 0.6 and later require Python 3.10+; 0.5.x is the last line supporting Python 3.9.
A loose requirement such as `ch-migrate-cli` or `~=0.5` upgrades past 0.5; pin
`ch-migrate-cli==0.5.1` until each project has run the command below.
After upgrading the package, run this in each migration project:

```bash
ch-migrate upgrade-env
```

`init` and `upgrade-env` write a thin version-2 environment that delegates to the
package. The old file is saved as `migrations/env.py.bak`; repeating the command
on the current shim leaves that backup intact. Review any old customizations.
Existing Python migrations and version tables continue to work without edits.
`up`, `down`, and `history` refuse old environments before connecting and tell
you to run `upgrade-env`; `status` still reports, so CI jobs that use it as a
non-blocking reporter keep passing.

One HTTP session spans a migration run, including later revisions in that run.
A standalone `SET` therefore carries into following revisions. Prefer a
statement's `SETTINGS` clause when the change should apply only to that query.
`session_timeout` in an environment or `defaults` sets the idle timeout in
seconds (default `1800`). If that session expires, the next request fails rather
than silently recreating a session with default settings.

## Quick start

Use a dedicated ClickHouse test server. The example uses local HTTP; replace the host and port with your server's address. For HTTPS, set `secure: true` and the HTTPS port (usually `8443`). Never run a trial migration against a shared or production database.

### 1. Initialize

```bash
mkdir my-clickhouse-project
cd my-clickhouse-project
ch-migrate init --name my_project
```

`init` writes `config.yaml`, `access.yaml` and the `migrations/` directory.

### 2. Configure the server

Replace `config.yaml` with:

```yaml
project:
  name: my_project

defaults:
  port: 8123
  secure: false
  admin_user: default

environments:
  dev:
    host: 127.0.0.1
    database: my_project_dev
    migration_user: migration_dev
```

Create `.env.local` in this project directory. Replace both values: the admin password is your server's existing password; the migration password is the password to give the new migration user.

```dotenv
CH_DEV_ADMIN_PASSWORD=your-admin-password
CH_DEV_MIGRATION_PASSWORD=your-migration-password
```

Keep `.env.local` out of git. `init` creates an ignore entry for it.

### 3. Bootstrap and create a migration

```bash
ch-migrate bootstrap dev --dry-run
ch-migrate bootstrap dev
ch-migrate new dev add_status --table logs
```

`bootstrap` logs in as the admin and creates the database, the migration role and the migration user. The role gets the `core` profile that `access.yaml` names: what migrations need on the project's database, and no right to manage users or roles. `bootstrap` needs ClickHouse 25.8 or later.

The last command creates a revision plus two files under `migrations/sql/history/tables/logs/`. Their names include a timestamp and revision ID. Replace the contents of the generated `.up.sql` with:

```sql
CREATE TABLE IF NOT EXISTS {db}.logs (id UInt64)
ENGINE = MergeTree ORDER BY id;
ALTER TABLE {db}.logs ADD COLUMN IF NOT EXISTS status String;
```

Replace the contents of its `.down.sql` with:

```sql
DROP TABLE IF EXISTS {db}.logs;
```

Do not edit the generated revision file. The example downgrade drops the table and its data; only use it in this empty test project.

### 4. Apply, inspect, and revert

```bash
ch-migrate up dev
ch-migrate status dev
ch-migrate history dev
ch-migrate down dev
```

After `up`, the `logs` table has `id` and `status` columns and status reports one applied revision. After `down`, the example table is gone.

### 5. Declare users and roles

Users and roles other than the migration identity go in `access.yaml`; its comments show the format. After adding some, preview the changes, then make them as the admin:

```bash
ch-migrate access plan dev
ch-migrate access apply dev
```

See [Access: users, roles and grants](#access-users-roles-and-grants).

## Concepts

### Project layout

```text
project/
├── config.yaml
├── access.yaml                # Users, roles and grants
├── .env.local                 # Secrets, ignored by git
├── alembic.ini
└── migrations/
    ├── env.py                 # Generated Alembic environment
    ├── script.py.mako
    ├── versions/              # Revision graph; generated Python adapters
    └── sql/
        ├── bootstrap/         # Optional bootstrap SQL
        └── history/
            ├── tables/<name>/
            ├── views/<name>/
            ├── dictionaries/<name>/
            └── other/         # No named object
```

`new` creates `<YYYY_MM_DD_HHMM>_<revision>_<slug>.up.sql` and `.down.sql`. The message slug is at most 40 characters. Object directories are created when needed. Only one of `--table`, `--view`, and `--dict` may be supplied.

### SQL files and placeholders

`run_sql` runs one statement per request. Semicolons inside strings, quoted identifiers, comments, or heredocs do not split statements. Empty or comment-only files fail instead of recording an unfilled migration as applied. Execution stops at the first failed statement; ClickHouse DDL is not transactional, so earlier changes remain.

| Placeholder | Value |
|---|---|
| `{db}` | Environment database |
| `{cluster}` | Configured cluster, or an empty string |
| `{on_cluster}` | `ON CLUSTER <cluster>`, or an empty string |

Keyword arguments to `run_sql` add or override substitutions. All other braces remain literal, including JSON and ClickHouse parameters such as `{id:UInt64}`. Doubled braces are not format escapes. Write statements that are safe to repeat where possible, such as `CREATE ... IF NOT EXISTS` and `DROP ... IF EXISTS`.

To render without executing, set `CH_ENVIRONMENT` and run `alembic upgrade head --sql`. Existing projects need `ch-migrate upgrade-env` to use the packaged environment. The package requires Alembic 1.18 or later and the official `clickhouse-connect[alembic]` integration.

### The version table

New `alembic_version` tables use `ORDER BY version_num`. The runtime inspects
`system.databases.engine` and the environment's `cluster` setting:

| Deployment | New version table |
|---|---|
| Shared database on ClickHouse Cloud | `MergeTree`; Cloud supplies its shared implementation |
| Self-hosted `Replicated` database | `ReplicatedMergeTree()` with server-default arguments |
| Atomic database with `cluster` configured | `ReplicatedMergeTree` created `ON CLUSTER`, with a Keeper path containing the database name and shard macro |
| Single server | `MergeTree` |

The official Alembic implementation advances a version by **inserting the new
row, then deleting the old row with `mutations_sync = 2`**. It does not issue an
asynchronous version UPDATE. If the process stops while the delete is pending,
both rows remain and Alembic refuses their overlapping history rather than
repeating the migration. Let the version mutation finish, then retry. A
server-reported failed version mutation stops the run with its reason; ch-migrate
never kills that mutation.

Before reading replicated heads, the tool catches up replicated database
metadata and inserted version parts. The table barrier uses
[`SYSTEM SYNC REPLICA ... LIGHTWEIGHT`](https://clickhouse.com/docs/reference/statements/system#sync-replica),
which does not wait for held mutation tasks. Failure checks cover reachable
hosts in the configured cluster, or the
[Replicated database's automatically named cluster](https://clickhouse.com/docs/reference/engines/database-engines/replicated).
An unavailable host is not evidence that its state is healthy.

Self-hosted replicas need Keeper and suitable `{shard}`/`{replica}` macros.
Configure authentication for a Replicated database's automatic cluster through
its `collection_name` setting; the named collection uses `cluster_username`,
`cluster_password`, and optionally `cluster_secret`/`cluster_secure_connection`.
Keep those values in server-side secret configuration, not migration files.
Provision the migration user on each node. Re-run bootstrap when upgrading to
grant the system-table reads and available `CLUSTER`, remote-read, and replica
synchronization privileges used by these checks.

Existing 0.4.1 `ReplacingMergeTree ORDER BY updated` tables are **never converted
automatically**. `upgrade-env` remains offline: it prints a conditional advisory
instead of probing every configured environment. `status ENV` performs the live
check and warns when existing state is not replicated but the deployment is.
Do not route migrations across nodes until that state is reconciled.

For a manual conversion:

1. Stop every migration runner. Back up the old table's DDL and rows on each
   node, and inspect unfinished version mutations.
2. Reconcile one authoritative set of completed revision heads with the
   revision graph. Do not infer it from the newest timestamp or blindly use
   `FINAL`: the old timestamp key can collapse heads written in the same second.
3. Keep the old tables as backups. On an Atomic cluster, rename each existing
   old table on its own node, then create the replacement on the cluster.
   In a Replicated database, issue schema changes once and let its DDL log
   propagate them.
4. Use `MergeTree ORDER BY version_num` on a single server, or the replicated
   engine from the table above. For an Atomic cluster, use a fresh shared Keeper
   path containing the database and the appropriate replica macros.
5. Copy the reconciled, distinct heads **once** into the empty replacement,
   synchronize replicas, and compare `status` through every routing endpoint.
   Resume runners only after they agree. Keep backups until the result is verified.

Offline SQL cannot inspect a live database engine: it uses configured `cluster`
information or the single-server/Cloud default for version-table DDL. Review
that DDL before using it for a self-hosted Replicated database. Cloud qualification
is separate from the local suite; the local replicated harness has one shard.

### Migrations that wait

`up` doesn't record a migration as applied until the work its statements started
has finished. After each statement that can write, it polls `system.mutations` on
the same checked ClickHouse session, so earlier `SET` statements stay in effect.
`op.execute`, raw `op.get_bind()` execution and `run_sql` all pass through that
check. Upgrades run in the CLI process, so killing it can't leave a child Alembic
runner advancing revisions. `down` waits the same way.

```bash
ch-migrate up dev                 # No default waiting timeout
ch-migrate up dev --timeout 120   # One waiting budget across the invocation
```

What `up` waits for:

- **Every unfinished mutation in the project database,** including one another
  writer started. Before the first migration runs, `up` waits for any already
  queued, and a failed one blocks the run. The version table's own mutations are
  left to [its bookkeeping](#the-version-table).
- **Each replica that has to apply it.** Zero parts left isn't completion on its
  own: the mutation must report `is_done`. A ReplicatedMergeTree mutation must be
  finished on every replica, and a replica that hasn't received it yet is still
  pending. Configure `cluster` for self-hosted replicated tables, or use a
  Replicated database's automatic cluster; without either, `up` refuses a table
  with more than one replica. SharedMergeTree on ClickHouse Cloud reports one
  shared mutation.
- **`ON CLUSTER` DDL on every host.** `up` sends these statements with
  `distributed_ddl_output_mode = 'throw'`, so ClickHouse answers once every host
  has run the statement, and a host that fails or doesn't finish in time fails the
  migration with ClickHouse's message. The limit is 270 seconds, below the
  client's read timeout, or what's left of `--timeout` if that's less. Mutations
  the statement starts are then waited for as above.

Statements ClickHouse already holds until they finish, such as a type-changing
`MODIFY COLUMN`, a `MODIFY TTL` that materializes, or a lightweight `DELETE`,
still hold their request as they did in 0.6.

While it waits, `up` shows progress on stderr. In a terminal, each mutation gets a
live bar weighted by bytes, with its parts and an estimate of the time left:

```text
→ Applying 3f9c2a1b  backfill_digest
  ━━━━━━━━━━━━━━━━━━━━╺━━━━━━━━━━━━━━━━━  18.2 / 41.0 GiB · 23/41 parts · 1:12 · ~1:30 left
✓ 3f9c2a1b  backfill_digest  mutation_7.txt · 41 parts · 41.0 GiB · 2:03
```

Self-hosted replicated tables get one row per replica. When output isn't a
terminal (CI logs, pipes, coding agents), `up` prints plain lines instead: one
when a wait starts, one at each 10% of the parts or every 30 seconds, and one
when it finishes:

```text
→ Applying 3f9c2a1b  backfill_digest
  waiting on mutation_7.txt (demo_dev.events): 0/41 parts
  waiting on mutation_7.txt (demo_dev.events): 5/41 parts · 0:14
✓ 3f9c2a1b  backfill_digest  mutation_7.txt · 41 parts · 2:03
```

Part sizes come from `system.parts`. If the migration role can't read it, the bar
counts parts only and says so once; progress never changes whether `up` succeeds.

**When `up` stops short.** After a timeout, Ctrl-C, dropped connection, killed
process or failed statement, ClickHouse keeps working and the migration isn't
recorded. `up` and `down` note each migration in `_ch_migrate_in_progress` when it
starts and clear the note once its version is recorded, so the next run knows the
migration was interrupted. That run waits for unfinished work, then:

- **Runs the migration again on its own** when every statement in it passes the
  idempotency gate without a waiver, so a repeat leaves the same result.
- **Stops before sending anything** when the migration has a waived statement or
  SQL the gate can't read, because that statement may already have applied. It
  names each one. Check what the earlier run changed, fix the SQL if needed, then
  name the revision: `ch-migrate up ENV --rerun-interrupted REV` (or `down`), or
  `-x rerun_interrupted=REV` with `alembic`. Naming it accepts the repeat.

Every `UPDATE`, `DELETE` and `INSERT ... SELECT` needs a waiver, so an interrupted
backfill always takes that second path.

**Failed mutation.** ClickHouse's `latest_fail_reason` stops the run, and `up`
prints the scoped `KILL MUTATION` statement for an operator; ch-migrate never
runs it. ClickHouse keeps retrying a failed mutation, and later mutations on that
table queue behind it. If the cause is temporary, such as memory or disk, fix it
and run `up` again. If the statement itself is wrong, kill the mutation, fix the
SQL and run `up` again, naming the revision with `--rerun-interrupted` if it's
waived. Killing a mutation doesn't undo the parts it already rewrote.

Run one migration runner per database, using a CI concurrency key or an external
lock; the in-progress notes aren't a lock. Re-run `bootstrap` when upgrading a
restricted migration user to obtain the `system.replicas` and `system.parts` grants.

### Re-runnable migrations

`up` statically checks pending upgrade statements before Alembic executes any of
them. Idempotency and standalone-SET errors refuse the whole run and print the source file, line,
statement, and suggested fix. There is no command-line bypass; `--skip-mv-check`
does not skip this gate. Other findings are warnings in `up`; standalone `lint`
retains their configured severities. A direct `alembic upgrade` runs the same gate
from `env.py` before its first migration.

The gate reads SQL in `run_sql` files and in literal strings, f-strings and
`text("...")` passed to `op.execute`, or to `execute` and `exec_driver_sql` on a
connection such as `op.get_bind()`. SQL that only exists at run time, such as a
variable, a built string or `run_sql(name)`, can't be checked, so it needs a
reasoned waiver like any other statement the gate can't vouch for.

Static lint rejects a standalone `SET` when `migrations/env.py` lacks the
`CH_MIGRATE_ENV_VERSION = 2` marker: that statement would be ignored on the old
connection. Run `ch-migrate upgrade-env`, or put the setting in the relevant
statement's `SETTINGS` clause. Version-2 projects allow `SET`; `SETTINGS` clauses
and the word `SET` inside string literals are not flagged. `standalone_set` is
a gate rule whose severity cannot be lowered.

Use `IF NOT EXISTS` for `CREATE` and `ALTER ... ADD`, and `IF EXISTS` for `DROP`,
`ALTER ... DROP`, `RENAME COLUMN`, and table, dictionary, and database renames.
This includes tables, views, materialized views, dictionaries, databases, users,
roles, row policies, settings profiles, quotas, functions, and named collections.
Column, index, projection, and constraint changes are checked separately.
`CREATE OR REPLACE` is accepted. `ATTACH` and `DETACH` of an object need their
matching `IF` form.

Each finding says how to make its statement safe to run twice, using the
statement's own names: the exact `IF` form, or what a retry would do and the
usual fix. For example, a backfill into a table that only its migration writes
can start with `TRUNCATE`, and `ATTACH PARTITION ... FROM` can usually be
`REPLACE PARTITION ... FROM`.

Inserts, exchanges, updates/deletes, `ATTACH PARTITION ... FROM`,
`MOVE PARTITION ... TO TABLE`, and attaching or detaching a single `PART` need a
reasoned waiver. Put it directly above the statement, without a separating blank
line:

```sql
-- ch-migrate: allow-non-idempotent This backfill targets a reviewed deduplicating table
INSERT INTO {db}.target SELECT id FROM {db}.source;
```

For inline Python SQL, use the same directive in a `#` comment directly above
the call that sends it. Empty reasons remain errors. Waivers print as INFO with their
reasons in `lint` and `up`; they do not make the statement idempotent.
`MODIFY`, `MATERIALIZE`, `CLEAR COLUMN`, `TRUNCATE`, `OPTIMIZE`, `SYSTEM`, grants,
revokes, `REPLACE PARTITION`, attaching or detaching a whole partition, and
comments are not flagged by this rule.
The reviewable classification corpus is `tests/corpus/idempotency.yaml`.

**Recovery:** fix the cause of a partial failure, then run `ch-migrate up ENV`
again. Earlier idempotent statements can run again; the version advances only
when the revision completes. A dropped or renamed column stays locked until that
change's mutation finishes, and `up` waits for it before running the next
statement, so a retry after a later failure doesn't hit "affected by mutation ...
which is not finished yet". If `up` was stopped while it waited, run it again: it
waits for that mutation, then runs the revision again, unless the revision has a
statement the gate can't vouch for (see below).

**Existing history:** `upgrade-env` records the current local script head(s) in
`config.yaml` as `lint.gate_baseline`, preserving YAML comments. It records
script history, not the deployed database head. The baseline and its ancestors
are exempt in every environment, including where they have not yet run.
Multiple heads are stored as a list. `init` writes no baseline, so new projects
gate every revision.

Review and commit the baseline diff. Running `upgrade-env` again updates it to
the current script heads; do not use that operation to hide new findings.
Static `lint` checks revisions after the baseline, and live `lint ENV` intersects
that scope with pending revisions. Lowering a gate rule through `lint.rules`
is an error: use a reviewed baseline or an explicit statement waiver instead.

### Irreversible migrations

```bash
ch-migrate new dev drop_legacy --table logs --irreversible "Drops legacy data"
```

This creates only an upgrade file. Its revision has an `irreversible` reason and raises `IrreversibleMigration` in its downgrade. An empty reason is rejected.

`down` reads markers statically, without importing migration files. It refuses an entire known range if any revision is irreversible: no preceding reversible downgrade runs first. It understands `-N` on linear history, full or unique-prefix IDs, and `base`. If a range is unknown, including a relative target across a merge point, it prints a note and relies on the migration's exception. Direct Alembic calls rely on the same backstop.

There is no override flag. To revert past a marked revision, implement its downgrade and remove the marker in a reviewed change. `irreversible = True` is accepted as "(no reason given)".

### Python migrations

Use `new --python` for logic that cannot be expressed as SQL files. It retains the Python template and, with an object option, a single SQL history file. Existing Python migrations continue to work.

```python
from ch_migrate import get_db, run_sql

def upgrade():
    run_sql("history/tables/logs/001_add_status.up.sql", db=get_db())
```

`get_db()` returns the environment database. `read_sql(path, **values)` still returns a string using Python `str.format`; unlike `run_sql`, callers must escape literal braces and execute the returned SQL themselves. For a single-statement file, the existing pattern remains valid:

```python
from alembic import op
from ch_migrate import get_db, read_sql

def upgrade():
    op.execute(read_sql("history/tables/users/001_create.sql", db=get_db()))
```

A hand-written irreversible Python revision uses both the marker and backstop:

```python
from ch_migrate import IrreversibleMigration

irreversible = "Drops legacy data"

def downgrade():
    raise IrreversibleMigration(revision, irreversible)
```

### Exchange and dictionary patterns

`new --exchange --table NAME` generates the existing shadow-table, copy, exchange, and drop scaffold. Coordinate or pause writers: this copy-and-swap pattern alone does not preserve inserts arriving during the copy. It is not an online-rebuild guarantee. Review the generated SQL and column mapping before applying it. The scaffold is marked irreversible because it drops the old table.

The generated copy and exchange statements require explicit, reasoned waivers
before `up` accepts them. The generator does not waive them automatically.

For a controlled change, the underlying pattern is:

```sql
CREATE TABLE IF NOT EXISTS {db}.users_shadow
(id UInt64, email String, phone String) ENGINE = MergeTree ORDER BY id;
INSERT INTO {db}.users_shadow SELECT id, email, '' FROM {db}.users;
EXCHANGE TABLES {db}.users AND {db}.users_shadow;
DROP TABLE IF EXISTS {db}.users_shadow;
```

`EXCHANGE TABLES` requires a supporting database engine. Neither the copy nor the exchange is automatically idempotent.

`create_dictionary` creates a dictionary from its SQL file, filling in `{db}` and `{password}`, the password of the user the dictionary reads its source as:

```python
from ch_migrate import create_dictionary

def upgrade():
    create_dictionary("history/dictionaries/dict_users/001_create.sql")
```

```sql
CREATE DICTIONARY IF NOT EXISTS {db}.dict_users (id UInt64, name String) PRIMARY KEY id
SOURCE(CLICKHOUSE(DB '{db}' TABLE 'users' USER 'dict_reader' PASSWORD '{password}'))
LAYOUT(FLAT()) LIFETIME(300)
```

The password comes from `DICT_READER_PASSWORD` if it is set, and otherwise from the environment's `dict_reader_password` secret: its `ssm:` path in `config.yaml`, or `CH_<ENV>_DICT_READER_PASSWORD`. If the statement fails, `up` hides the password in what it prints, `--verbose` included.

- **Without `access.yaml`,** the helper first grants `SELECT` on the source table to a user named `dict_reader`, as 0.8 does.
- **With `access.yaml`,** it grants nothing. Declare the reader and its `SELECT` there, with the same secret, and run `access apply` before `up` so the reader can read the source once the dictionary exists:

```yaml
users:
  dict_reader:
    password: {secret: dict_reader_password}
    grants:
      - SELECT ON {db}.users
```

### Access: users, roles and grants

`access.yaml`, next to `config.yaml`, declares the users and roles a project needs, the roles each one holds and their grants. `ch-migrate access plan ENV` shows how a server differs from it, and `access apply ENV` makes the server match. `init` writes one that declares only the migration role's profile, with commented examples.

ch-migrate deals with three identities:

| Identity | Managed by | Declared in |
|---|---|---|
| The admin, `admin_user` | You. ch-migrate never changes it. | `config.yaml` only. `access.yaml` can't declare it. |
| The migration role and user | `bootstrap` and `access apply`, which only add to them | A built-in profile, plus `migration_role.grants` |
| Every other user and role | `access apply`, which makes each one match the file | `access.yaml` |

```yaml
migration_role:
  profile: core          # core (the default) or legacy: 0.8's grants
  grants:                # beyond the profile
    - CREATE FUNCTION, DROP FUNCTION ON *.*

roles:
  analyst:
    grants:
      - SELECT, SHOW TABLES ON {db}.*
  loader:
    grants:
      - INSERT, SELECT ON {db}.events
  reporting:
    roles: [analyst]     # a role can hold roles

users:
  dashboards:
    password: {secret: dashboards_password}
    roles: [reporting]
  etl:
    password: {secret: etl_password}
    roles: [loader]
    default_roles: [loader]
    grants:
      - SELECT ON {db}.regions

environments:
  production:
    users:
      etl: {absent: true}
```

- **Grants** use ClickHouse's own words: `<privileges> ON <target> [WITH GRANT OPTION]`. A target is `*.*`, `db.*` or `db.table`. `READ` and `WRITE` take a source as their target, as in `READ ON REMOTE`; the old spelling, `REMOTE ON *.*`, is an error that names the new one. An entry can also be `{grant: "...", best_effort: true}` (see below). Column grants and prefix targets such as `db.t*` aren't supported.
- **Placeholders** `{db}`, `{project}`, `{env}` and `{migration_role}` are filled in for each environment.
- **Roles held.** `roles:` on a user or a role grants it those roles. `default_roles:` picks which of a user's roles are default roles; without it, all of them are. A role the file names but doesn't declare must already exist on the server, or `plan` exits 2.
- **Passwords.** `password: {secret: NAME}` reads the secret the way the migration password is read (see [Secrets](#secrets)). A password is set only when its user is created, or again with `access apply --reset-passwords`, and ch-migrate never reads one back. It goes to the server as a bound parameter in the request body, never in the statement or the URL, and an error ch-migrate prints hides every secret it has read. A declared user with no `password` is never created: it must already exist, or `apply` exits 2 before any change.
- **Environments.** `environments.<env>` merges over the rest of the file: mappings merge and lists replace.
- **`absent: true`** drops a user or role; in an environment block, in that environment only. Renames aren't supported: declare the old name absent and add the new one.
- **Validation.** `access validate ENV` checks the file offline, with no server and no secrets. Unknown keys are errors, and each error names the file, the key path and the reason. A privilege name the bundled ClickHouse 26.3 list doesn't know is a warning; `plan` checks names against the live server.

**What `apply` changes.** For each user and role the file declares, `apply` makes its grants and roles match the file exactly. It grants first and revokes after. Revokes, replaces (below) and default-role changes wait for every earlier change to succeed: creates, password resets, role memberships, grants, replaces and default roles. If one failed, for any user or role, they're all skipped and each is named in a `skipped:` warning, so old access survives, and `apply` exits 1. For example, a user moving from role `A` to role `B` keeps `A` when granting it `B` fails, when a grant to `B` itself fails, and when the user holds both and a grant to `B` meant to replace one of `A`'s fails. Holding back the default-role change means a role the user keeps stays active at login. A role's replace runs before the replace of any user or role that holds it, whatever the order in `access.yaml`, so a holder's replace waits for it. Users and roles the file doesn't declare are never touched, and one is dropped only when it's declared `absent: true`. Run one `apply` at a time for an environment: two at once can interleave their statements, as two `bootstrap` runs could in 0.8. `plan` lists every revoke and drop before `apply` runs it.

Narrowing a grant's scope, such as `db.*` to `db.events`, can't grant first, because ClickHouse folds the narrower grant into the broader one. Instead, `apply` sends one `GRANT ... WITH REPLACE OPTION` of that user's or role's whole declared set, which keeps the roles it holds. A declared best-effort grant goes into the replace only as far as it's held now, since the admin may not hold the rest, and the replace keeps what is held inside that grant's scope: with a best-effort `SELECT ON system.*` declared, a held `SELECT ON system.tables` survives. That needs the admin to hold, with grant option, every right the grantee holds now; if it doesn't, nothing changes and `apply` says why. One statement can't keep a revoke inside a best-effort grant's scope, such as `SELECT ON system.*` held less `system.users`, nor a revoke of only the grant option there when the declared grant carries one. Then the replace isn't sent: it's skipped and named in a `skipped:` warning, the user or role keeps the access it has, and `apply` exits 1. As after a failure, its revokes and default-role change are skipped too, and so are the revokes, replaces and default-role changes of each user or role that holds it.

**Adopting existing users and roles.** Declaring a user or role that already exists adopts it. `access show ENV NAME...` prints what the server holds for those names in `access.yaml` form, as a starting point; write it to another file and copy from there. An `access.yaml` that is empty, or only comments, is an error: delete it to keep legacy mode. `plan` marks each declared user or role that exists and has changes pending with `~`, then lists the changes; ch-migrate keeps no state, so adoption and drift look the same. Review the first `plan` before the first `apply`: it revokes every grant the file leaves out.

**The migration role** holds a built-in profile:

- **`core`**, the default, holds what migrations need on the project's database, with no grant option and no user or role management:
  - on `{db}.*`: `CREATE TABLE`, `DROP TABLE`, `UNDROP TABLE`, `ALTER`, `CREATE VIEW`, `DROP VIEW`, `CREATE DICTIONARY`, `DROP DICTIONARY`, `SELECT`, `INSERT`, `DELETE`, `TRUNCATE`, `OPTIMIZE`, `SHOW TABLES`, `SHOW COLUMNS` and `SHOW DICTIONARIES`;
  - `SELECT` on `system.databases`, `system.tables`, `system.mutations` and `system.dictionaries`;
  - best-effort: `SELECT` on `system.parts` and `system.replicas`, `SYSTEM SYNC REPLICA` and `SYSTEM SYNC DATABASE REPLICA` on `{db}.*`, and `CLUSTER`, `READ ON REMOTE` and `CREATE TEMPORARY TABLE ON *.*`.
- **`legacy`** is 0.8's migration role exactly: core's database rights with grant option, plus `CREATE FUNCTION` and `DROP FUNCTION`, user, role and row-policy management, and `SELECT` on `system.*` as far as the admin can give it. Set `migration_role.profile: legacy` when the project's migrations create users or roles, or grant to other users.

`migration_role.grants` adds grants beyond the profile. `bootstrap` and `apply` only ever add to the migration role. `plan` marks each grant the role holds beyond what's declared with `!`, which leaves the exit code alone; revoke those by hand. Because nothing is revoked from it, two project directories can share one migration role (see `migration_role` under [Configuration](#configuration)) without revoking each other's grants, and a project that adopts `access.yaml` keeps the 0.8 grants its migrations rely on. The migration role and user can't be declared under `roles:` or `users:`.

**Best-effort grants** land as far as the admin can give them, as `GRANT CURRENT GRANTS(...)`: where the admin holds part of a privilege, that part lands, where a plain `GRANT` would land nothing. A best-effort grant that is missing or only partly held is a warning in `plan`, `apply`, `check` and `bootstrap`, and never changes an exit code; only a missing required grant makes `plan` or `check` exit 1. `apply` tries the missing ones again on every run, so `plan` can report in sync while it warns. Where the server grants `CLUSTER` to every login, as ClickHouse Cloud does, `CLUSTER ON *.*` is already held: no command warns about it, and `apply` doesn't try it. On a self-hosted cluster, `up` needs four of core's best-effort grants: `CLUSTER`, `READ ON REMOTE`, `SYSTEM SYNC REPLICA` and, while a mutation on a replicated table is pending, `SELECT ON system.replicas`.

**Without `access.yaml`** a project is in legacy mode, as every project was in 0.8. `bootstrap` gives the migration role the `legacy` profile, and `mcp_user_name` and `dict_reader_name` in `config.yaml` create the read-only and dictionary roles and users that 0.8 creates. As in 0.8, each is made only when its password (`mcp_password`, `dict_reader_password`) is set; a name without one is skipped with a warning. `access plan` and `apply` compare only the migration role and user, and `apply` only adds missing `legacy` grants.

**Migrations and `access.yaml`.** In a core project, a migration can't create users or roles, or grant to another user: core has neither the rights nor grant option. Declare those users, roles and grants in `access.yaml`. When a migration and `access.yaml` both touch a declared user or role, `access.yaml` wins at the next `apply`: a grant a migration gives to a user or role that `access.yaml` declares is revoked then, unless `access.yaml` declares it too. With an `access.yaml`, `create_dictionary` grants nothing: declare the dictionary's reader and its `SELECT` in `access.yaml` (see [Exchange and dictionary patterns](#exchange-and-dictionary-patterns)). Set up a fresh environment with `bootstrap`, `up`, then `access apply`. A project whose migrations create dictionaries for a declared reader runs `access apply` before `up` instead, so the reader holds its `SELECT` when a dictionary first loads; ClickHouse accepts grants on tables that don't exist yet. Deploy with `up`, then `access apply`, and run `access plan` in CI to see any drift a migration introduced. ClickHouse accepts grants on databases and tables that don't exist yet, so `apply` can also run before `up`.

**`READ` and `WRITE` on a source.** ClickHouse keeps `READ` and `WRITE` on a source such as `S3` apart only with `access_control_improvements.enable_read_write_grants` on. That setting is off by default, and SQL can't see it; with it off, `READ ON S3` holds both directions. So `plan` and `apply` compare `READ` and `WRITE` on one source as one grant: declaring either covers both, and neither direction is revoked because the other is declared. `validate` warns about a grant that names one direction alone. A `WRITE` added by hand where `READ` is declared goes unnoticed.

**Clusters.** On a self-hosted cluster, users and roles live either in replicated storage (Keeper) or on each node's own disk. ch-migrate reads which from `system.user_directories`: the first directory there that can take a create. `users.xml`, LDAP and ClickHouse Cloud's `jwt` only read, so they're passed over.

- **On each node's own disk, with `cluster` set in `config.yaml`,** `access apply`, and `bootstrap` with an `access.yaml`, run every create, grant, revoke and drop `ON CLUSTER`, sent with `wait_end_of_query=1` so that a host that fails is reported. The cluster's name is backquoted, so a name such as `c-1` works. ClickHouse refuses `CURRENT GRANTS` with `ON CLUSTER`, so each best-effort privilege goes as its own plain `GRANT ... ON CLUSTER`: a full admin lands each one on every node, and each one the admin can't give is a warning. The admin needs `CLUSTER ON *.*` to run `ON CLUSTER` at all.
- **Only one node is read.** On each node's own disk, with `cluster` set, `access plan`, `access apply` and `bootstrap` with an `access.yaml` read only the node they connect to, and before any change they print a line naming it: the server's `hostName()`, or `host:port` from `config.yaml` when that can't be read. If an `ON CLUSTER` change lands on one node and fails on another, `apply` exits 1 and names the error, but the next `plan` or `apply` against the first node sees nothing to do, so a `plan` that exits 0 there says nothing about the other nodes. After a failed `ON CLUSTER` apply, point `host` in `config.yaml` at each node in turn and run `access plan` against it. Reading every node is a planned follow-up.
- **In replicated storage, and on ClickHouse Cloud,** nothing runs `ON CLUSTER`; with replicated storage it would fail with `ACCESS_ENTITY_ALREADY_EXISTS`. A replica can be behind, so `plan`, `apply`, `show` and `bootstrap` read every replica at once, through `clusterAllReplicas`: the cluster named by `cluster` in `config.yaml`, or else the one cluster that holds the node and another host (clusters that list the same hosts, as Cloud's `default` and `all_groups.default` do, count as one). A row missing on every replica is absent at once. Replicas that disagree are read again until they agree, for up to `access_read_timeout` seconds; if they still disagree then, one line names them, and what the node connected to holds counts. Where every replica can't be read, because no cluster is found, the admin lacks `READ ON REMOTE` or a replica can't be reached, a missing row on the node connected to is read again for up to `access_read_timeout` seconds before it counts as absent. A lone replicated node keeps this wait because peers sharing its users may exist outside its listed clusters. The fallback reason prints once, only if the command actually waits.
- **`bootstrap` without an `access.yaml`** never uses `ON CLUSTER`, even with `cluster` set, and sends best-effort grants as `CURRENT GRANTS`, as 0.8 does. Where users and roles are on each node's own disk, run it once per node.
- **`access validate --sql`** prints best-effort grants as `CURRENT GRANTS`, with no `ON CLUSTER`. Where users and roles are on each node's own disk, a DBA runs that SQL on each node.
- **`bootstrap --dry-run`** with an `access.yaml` prints every statement for the database and the migration role and user, without `ON CLUSTER`: offline, it can't tell whether `ON CLUSTER` applies, and its header says so. The real run sends only what's missing.

**What the admin needs.** `access show`, `plan`, `apply` and `check`, and `bootstrap`, need ClickHouse 25.8 or later; on an older server they exit 2 and name its version. `bootstrap`, `access show`, `plan` and `apply` log in as `admin_user`, which needs:

- `SELECT` on `system.users`, `system.roles`, `system.grants`, `system.role_grants` and `system.user_directories`, to read what the server holds;
- where users and roles are replicated, as on ClickHouse Cloud, `READ ON REMOTE` to read every replica at once, and `SELECT ON system.clusters` when `cluster` isn't set. Without them, a read waits for what it doesn't find;
- `CREATE DATABASE ON {db}.*`, even when the database exists, and `CREATE ROLE` and `CREATE USER` on `*.*`;
- `ROLE ADMIN ON *.*`, because creating a role doesn't give its creator the right to grant it. `ROLE ADMIN` also lets the admin grant any role to anyone, so even the smallest admin that can run `bootstrap` holds that power;
- each grant it gives, with grant option. The `SHOW` grants come with the admin's other rights on the database;
- `ALTER USER` to set default roles and reset passwords, and `DROP USER` and `DROP ROLE` to drop what `access.yaml` declares `absent: true`;
- `CLUSTER ON *.*` to run `ON CLUSTER`.

An admin with the read grants, the three creates, `ROLE ADMIN` and core's required grants with grant option lands every required grant of a core `bootstrap`, and each best-effort grant it can't give is a warning. `access check` needs no admin: it logs in as the migration user, for servers where ch-migrate has no admin login.

## Drift detection in CI

After applying and verifying migrations in an environment, capture its expected
schema:

```bash
ch-migrate snapshot dev
mv migrations/sql/snapshots/20261002_120000 migrations/sql/snapshots/dev
ch-migrate diff dev --snapshot-dir migrations/sql/snapshots/dev --json
```

`snapshot` writes a new timestamped directory every time; replace the example
timestamp with the one it reports. Give each environment's snapshot a fixed name,
such as `snapshots/dev`, so `CH_MIGRATE_SNAPSHOT_DIR` never changes and the next
snapshot shows up in review as a diff against this one. Review and commit that
directory alongside the migration head. Do not regenerate the snapshot
automatically in the drift job: doing so would accept an out-of-band change as the
new expected schema. Re-snapshot after an intentional migration or an explicitly
reviewed reconciliation, not merely to turn a failed check green. To re-snapshot,
run `snapshot` again, delete the old `snapshots/dev`, and move the new directory
into its place, so the commit also shows objects that were dropped.

Copy [the GitHub Actions example](docs/examples/github-actions-drift.yml) into the
consumer repository's `.github/workflows/`. It runs on a daily schedule and
same-repository pull requests; fork PRs are excluded because they have no database
secret. It does not use `pull_request_target`. All actions are pinned by commit.
The checkout must contain `config.yaml` and the committed snapshot, and the runner
must be able to reach the selected server.

Configure these repository variables:

| Variable | Value |
|---|---|
| `CH_MIGRATE_PACKAGE` | A pinned `ch-migrate-cli` requirement, such as `ch-migrate-cli==0.8.0`. 0.8.0 is the first release with `diff --json`; there is no fallback to an older one. |
| `CH_MIGRATE_ENV` | An environment name in `config.yaml`, such as `dev`. |
| `CH_MIGRATE_SNAPSHOT_DIR` | That environment's committed snapshot directory. |

Set the repository secret `DRIFT_MIGRATION_PASSWORD` to the password for that
environment's configured user. Prefer a dedicated schema-inspection account,
not an admin account. The job writes a private `.env.local` without printing the
value and removes the file even when comparison fails.

`diff --json` exits **0** for matching schemas, **1** for drift, and **2** for a
configuration/connection error. The workflow saves `drift.json`, uploads it even
on comparison failure, then fails the job for either nonzero result. The report
names changed tables and fields. ch-migrate's own test suite runs the example's
shell steps against a real ClickHouse server, installing from the source checkout,
for both a matching and a drifted schema.

Without `--snapshot-dir`, `diff` sorts entries in `migrations/sql/snapshots/` by
name and chooses the last one. It does not select a snapshot by environment or
recorded migration revision. Use an explicit path in CI, especially when one
repository holds snapshots for several environments.

## Command reference

Every command accepts `--help`. Top-level `ch-migrate --version` reports the installed package version. `ENV` below names an entry in `config.yaml`.

Output lines start with `→` for a step, `✓` for a result, `!` for a warning and `✗` for an error; warnings and errors go to stderr. Colour is dropped when output is not a terminal or `NO_COLOR` is set, and lines are never wrapped, so paths and SQL can be copied or grepped.

`status`, `history`, `lint`, and `diff` accept `--json`. JSON mode writes one document to stdout; diagnostics go to stderr. Every document includes `"schema_version": 1` and `"command"`. The format can still change before 1.0; `schema_version` says which one a document follows. Errors include an `error` string rather than inventing a successful empty result. The schemas linked below use JSON Schema draft 2020-12.

### `init`

`ch-migrate init [PATH] [-n NAME]` initializes the current directory by default. `-n/--name` sets the project name; otherwise it uses the directory name. It writes `config.yaml`, an `access.yaml` whose migration role holds the `core` profile, `alembic.ini`, `.env.local.example` and the `migrations/` directory, and leaves any of those files that already exists alone.

Example: `ch-migrate init analytics --name analytics`

### `bootstrap`

`ch-migrate bootstrap ENV [--dry-run] [-v]` creates the database and the migration role and user, then checks the role's grants, logged in as the migration user. With an `access.yaml`, the role gets its profile and `migration_role.grants`, and `access apply` makes the rest. It needs ClickHouse 25.8 or later. On a cluster that keeps users and roles on each node's own disk, with `cluster` set, it runs `ON CLUSTER` as `access apply` does, reads only the node it connects to, and prints a line naming that node. Without an `access.yaml`, `bootstrap` gives the grants 0.8 gave, plus the optional MCP and dict reader users, on the node it connects to and never `ON CLUSTER`, so such a cluster is bootstrapped once per node, as in 0.8. On a self-hosted cluster where users and roles are stored in Keeper, `bootstrap` never uses `ON CLUSTER`, with or without an `access.yaml`, so it creates the database only on the node it connects to, as 0.8 did; create it on the other nodes, or use a `Replicated` database. It creates the role and user first, attempts every grant and names each one that fails. `--dry-run` prints the SQL without connecting or reading a secret, with each password as a `<secret:NAME>` placeholder. With an `access.yaml` it prints every statement, without `ON CLUSTER`, since offline it can't tell what the server already holds or where users are stored; the real run sends only what's missing. `-v/--verbose` prints statements during execution. Requires admin and migration credentials. It exits 0 when done, 1 when something failed or, with an `access.yaml`, a change is still pending once replicas have had `access_read_timeout` to agree (each one named, as `access apply` names them), and 2 on an error before any change.

Example: `ch-migrate bootstrap dev --dry-run`

### `access`

`ch-migrate access validate ENV [--sql]` checks `access.yaml`, which declares users, roles and grants, and the built-in profiles, offline: it needs no server and no secrets. It exits 0 when the file is valid and 2 when it isn't, listing every error with its file, key path and reason. Warnings, such as a privilege name the bundled ClickHouse 26.3 list doesn't know, leave the exit code at 0. Without `access.yaml`, it checks legacy mode: the grants 0.8's `bootstrap` gave. `--sql` also prints the SQL a DBA can review and run. Each password is an unquoted placeholder, `<secret:NAME>`, so the SQL fails to parse until each one is replaced with its secret as a quoted string. A declared user with no password isn't created there; the header names it as a user that must already exist. Best-effort grants are printed as `GRANT CURRENT GRANTS(...)`, with no `ON CLUSTER`, so on a self-hosted cluster that keeps users and roles on each node's own disk, the DBA runs the SQL on each node.

Example: `ch-migrate access validate dev --sql`

`ch-migrate access show ENV [NAME...]` logs in as `admin_user` and prints what the server holds, in `access.yaml` form, for the users and roles `access.yaml` declares or for the names given. It is where adopting existing users and roles starts: write it to another file, such as `ch-migrate access show dev NAME > access.show.yaml`, and copy what you keep into `access.yaml`. Redirected straight into `access.yaml`, the shell empties that file before `show` runs, and `show` then stops with an error. It writes nothing, reads only the admin's secret and never shows a password. It exits 0, or 2 on an error.

`ch-migrate access plan ENV` logs in as `admin_user` and prints how the server differs from `access.yaml`, one line per change, without changing anything:

- `+` create or grant;
- `-` revoke or drop;
- `~` a declared user or role that already exists and has changes pending;
- `!` a grant the migration role holds beyond its profile. Nothing is revoked from the migration role.

It exits 0 in sync, 1 with changes pending and 2 on an error. `!` lines and warnings about best-effort grants leave the exit code alone. Narrowing a grant's scope, such as `db.*` to `db.events`, shows as one `replace` of that user's or role's declared set, and the line names what the replace takes away. `READ` and `WRITE` on one source, such as `READ ON S3`, compare as one grant: ClickHouse keeps them apart only with `access_control_improvements.enable_read_write_grants` on, which is off by default, so `validate` warns about a grant that names one direction alone. In legacy mode, with no `access.yaml`, only the migration role and user are compared. On a self-hosted cluster that keeps users and roles on each node's own disk, with `cluster` set, `plan` reads only the node it connects to and names it first: after a failed `ON CLUSTER` apply, run `plan` against each node (see Clusters under [Access: users, roles and grants](#access-users-roles-and-grants)).

Example: `ch-migrate access plan dev`

`ch-migrate access apply ENV [--reset-passwords]` logs in as `admin_user` and makes the changes `plan` lists, in order: creates, memberships, grants, replaces, default roles, revokes, then drops. A role's replace runs before the replace of any user or role that holds it, whatever the order in `access.yaml`. It continues past a failed statement, then plans again and reports what still differs. Revokes, replaces and default-role changes run only if every earlier change succeeded; otherwise they're skipped and named, so old access survives and a role the user keeps stays active at login. A user whose create or password reset fails gets nothing more from that run, since someone else may have made it, with a password that isn't the declared one. Drops run only if nothing failed and every replace was sent. Otherwise each drop is skipped and named, since a revoke held back may leave a user or role relying on a role about to be dropped. Narrowing a scope is one `GRANT ... WITH REPLACE OPTION` of the declared set, which keeps what is held inside a declared best-effort grant's scope. A replace that can't keep exactly what is held of a declared best-effort grant, such as `SELECT ON system.*` held less `system.users`, isn't sent: it's skipped and named, the user or role keeps its access, the changes waiting on it are skipped as after a failure, and `apply` exits 1. The migration role is only ever added to. Best-effort grants land as far as the admin can give them, and the rest is a warning. On a self-hosted cluster that keeps users and roles on each node's own disk, with `cluster` set, every statement runs `ON CLUSTER`, and each best-effort privilege is its own `GRANT ON CLUSTER`; `apply` reads only the node it connects to and names it before any change, so a change that failed on another node goes unseen by the next run against this one. A password is set when its user is created, and again with `--reset-passwords`, which also resets a user the same run creates, right after its create. Before any change, `apply` reads every secret it will set, and it exits 2 if `access.yaml` declares `admin_user` or the login it runs as, would revoke from, replace or drop a role that login holds, or declares a new user with no password. Interrupted after a write, it says so and exits 130, as `up` does. It exits 0 when the server then matches, 1 when it doesn't, because a change failed or was skipped (each one named) or something still differs, and 2 on an error before any change.

Example: `ch-migrate access apply dev`

`ch-migrate access check ENV` is for servers where ch-migrate has no admin login. It logs in as the migration user and runs one `CHECK GRANT` for each grant of the migration role's profile and `migration_role.grants`, as the flags each grant means on its target. `CHECK GRANT` can't see a grant option, so only privileges are checked. It writes nothing and reads only the migration user's secret. It exits 0 when every required grant is held, 1 when one is missing (each one named) and 2 on an error. A missing best-effort grant is a warning.

Example: `ch-migrate access check dev`

`show`, `plan`, `apply` and `check` need ClickHouse 25.8 or later. The admin needs `SELECT` on `system.users`, `system.roles`, `system.grants`, `system.role_grants` and `system.user_directories`, and where users and roles are replicated, `READ ON REMOTE`. `apply` also needs `ALTER USER` to set default roles and reset passwords, `DROP USER` and `DROP ROLE` to drop what `access.yaml` declares `absent: true`, and `CLUSTER ON *.*` where it runs `ON CLUSTER`. The full list, including the `ROLE ADMIN` that lets the admin grant any role to anyone, is under [Access: users, roles and grants](#access-users-roles-and-grants). Where users and roles are replicated, a replica can be behind, so every replica is read at once, and replicas that disagree are read again for up to `access_read_timeout` seconds (default 30, set per environment in `config.yaml`). Where every replica can't be read, a line says why, and a missing row is read again for that long before it counts as absent.

### `new`

`ch-migrate new ENV NAME [--table T | --view V | --dict D] [--irreversible REASON | --python | --exchange]` creates SQL-first migrations by default. Object-option aliases are `-t`, `-v`, and `-d`. `--python` keeps the Python template. `--exchange` requires `--table`. The three authoring-mode options are mutually exclusive, and conflicts fail before a revision is written.

Example: `ch-migrate new dev add_status --table logs`

### `up`

`ch-migrate up ENV [-r REV] [--timeout SECONDS] [--skip-mv-check] [--verbose]` applies migrations to `head` by default after the idempotency gate passes, printing one line per migration. `-r/--revision` selects a target. `--timeout` is a finite positive waiting limit; omitting it waits without a deadline. `--skip-mv-check` skips nonblocking materialized-view declaration checks, not the idempotency gate; use it only after reviewing those findings. If a migration fails, `up` names it, the SQL file, the statement and its line, and ClickHouse's error; `--verbose` adds the Python traceback.

Example: `ch-migrate up dev --revision abc123`

### `down`

`ch-migrate down ENV [-r REV] [--verbose]` reverts one revision by default (`-1`). `-r/--revision` accepts another target. Known ranges containing irreversible revisions are refused. Failures are reported as for `up`.

Example: `ch-migrate down dev --revision base`

### `status`

`ch-migrate status ENV [--json]` shows connection information, applied/pending counts, and head status, and names the `up` command when migrations are pending. Status is a report: human output exits 0 when the database is unreachable (with a warning) or migrations are pending, and exits 1 only when the configuration or `migrations/versions/` is missing, so CI can run it as a non-blocking check. JSON mode uses nonzero exits for unreachable or pending state.

Example: `ch-migrate status dev`

With `--json`, [the status schema](docs/schemas/status.schema.json) includes
`database`, `current_heads`, `script_heads`, `pending`, `applied`, and `at_head`.
`applied` includes ancestors resolved through the revision graph, not just stored
heads. JSON exits 0 when the head sets match exactly, 1 when pending or diverged,
and 2 when configuration or database state cannot be read. Human-mode exits are
unchanged.

```bash
ch-migrate status dev --json | jq -e .current_heads
# Check the actual head set, not the last element of applied:
ch-migrate status dev --json | jq -e '.current_heads == ["PINNED_REVISION"]'
```

Replace `PINNED_REVISION` with the required revision. In a shell pipeline, enable
`set -o pipefail` if the caller must also preserve ch-migrate's exit code.

### `history`

`ch-migrate history ENV [--json]` displays the revision graph and applied state.

Example: `ch-migrate history dev`

`ch-migrate history dev --json` follows [the history schema](docs/schemas/history.schema.json).
Each entry in `revisions` includes its revision, all `down_revisions` (including
merge parents), description, create date, path, applied status, and irreversible
reason. A reversible revision has `irreversible: null`. If database state is
unavailable, `applied` is null, the document includes `error`, and JSON exits 2.
An unknown database head also makes applied status unknown instead of claiming
that every local revision is unapplied.

### `lint`

`ch-migrate lint [ENV] [--json]` analyzes upgrade statements, not downgrade SQL. Without `ENV`, it checks revisions after the gate baseline statically without credentials or a connection. With an environment, it checks only pending revisions in that scope and adds live size and dependency checks. If it cannot determine the pending set, it fails rather than silently checking a different scope. Errors exit nonzero; warnings and waiver INFO lines alone do not.

Example: `ch-migrate lint`

`ch-migrate lint --json` follows [the lint schema](docs/schemas/lint.schema.json).
`findings` include rule, severity, source file/line, message, `blocking`, and
`waived`. `blocking` means the finding currently blocks `up`; a waived finding
has its written reason and does not block. `counts` reports total, error, warning,
info, blocking, and waived findings. JSON and human output use the same exit
codes: 1 for lint/configuration errors, otherwise 0.

Findings name the project-relative SQL file and statement line. Inline Python SQL
points to its `op.execute` call. Extraction reads `run_sql`/`read_sql` file
references and literal or f-string `op.execute` arguments without importing
revisions. It preserves placeholders and adjacent comments; arbitrary Python
expressions are not evaluated. Materialized-view declaration and companion-grant
validation still uses the complete migration batch, with lint findings limited
to selected upgrade statements.

### `deps`

`ch-migrate deps ENV [-v PATH]` reads the live materialized-view and dictionary dependency graph. `-v/--validate PATH` checks a SQL file against it.

Example: `ch-migrate deps dev --validate migrations/sql/history/tables/logs/change.up.sql`

### `diff`

`ch-migrate diff ENV [-s PATH] [--json]` compares the live schema with the latest snapshot. `-s/--snapshot-dir PATH` chooses another snapshot. Exit code 0 means no drift; 1 means drift. JSON errors exit 2; human-mode execution errors retain exit 1.

Example: `ch-migrate diff dev --snapshot-dir migrations/sql/snapshots/20261002_120000`

`ch-migrate diff dev --json` follows [the diff schema](docs/schemas/diff.schema.json).
`objects` identifies each object's type, name, status, and structural `details`
(field, local value, remote value, and message). `in_sync` is true only when every
object matches. Use the exit code as the drift gate; an error document does not
contain a fabricated `in_sync: true`.

### `snapshot`

`ch-migrate snapshot ENV [-e GLOB] [-f GLOB]` writes CREATE statements to a timestamped snapshot directory. `-e/--exclude` and `-f/--filter` accept repeated glob patterns for excluded and included objects.

Example: `ch-migrate snapshot dev --exclude 'temp_*' --filter 'logs*'`

### `rebase`

`ch-migrate rebase ENV [--onto REV] [--dry-run]` rewrites dangling local revision branches onto the deployed head. `--onto` selects an explicit target; `--dry-run` previews changes. Review the preview before rewriting migration history; do not rewrite deployed revisions.

Example: `ch-migrate rebase dev --onto abc123 --dry-run`

### `upgrade-env`

`ch-migrate upgrade-env` replaces `migrations/env.py` with the installed version, backs up the old file as `env.py.bak`, and records current script heads as `lint.gate_baseline` while preserving YAML comments. No command-specific options. Review the baseline diff. It does not merge: any local customizations (connection settings, session pins, hooks) are dropped from the new file. Reapply them from the backup, or skip `upgrade-env` and edit a customized `env.py` by hand. An unchanged shim is not backed up again.

Example: `ch-migrate upgrade-env`

### `skill`

`ch-migrate skill [--user | --project]` installs the bundled Claude skill. `--user` is the default (`~/.claude/skills/ch-migrate/`); `--project` writes `./.claude/skills/ch-migrate/`.

Example: `ch-migrate skill --project`

## Configuration

`defaults` are merged with each environment. Environment fields override defaults. The project name controls role names. A Cloud/HTTPS example:

```yaml
project:
  name: analytics

defaults:
  port: 8443
  secure: true
  admin_user: default
  # cluster: my_cluster
  # migration_role: shared_migration_role
  # access_read_timeout: 30
  # dict_reader_name: dict_reader
  # mcp_user_name: mcp_reader

environments:
  dev:
    host: your-service.clickhouse.cloud
    database: analytics_dev
    migration_user: migration_dev
    aws_region: us-east-1
    ssm:
      admin_password: /analytics/dev/admin_password
      migration_password: /analytics/credentials#password
      dashboards_password: /analytics/dev/dashboards_password
```

These keys can be set in `defaults` or in an environment:

- `migration_role` names the migration role. Without it, the role is `<project>_migration_role`. Two project directories that set the same `migration_role`, each with its own database, share that role: each directory's `bootstrap` adds the profile's grants on its own database and revokes nothing, and each keeps its own version table. The role has no rights on a database whose directory hasn't been bootstrapped, so a stray migration there fails with `ACCESS_DENIED`.
- `access_read_timeout` is a deadline, in seconds from the first read, for the `access` commands and `bootstrap` to keep reading again where users and roles are replicated: until every replica agrees, or, where every replica can't be read, before a missing row counts as absent. The time the reads take counts toward it, as well as the waits between them, so slow reads can't stretch it; one last read may start at the deadline, and each of its requests is bounded by the driver's request timeout. It must be a finite number, 0 or more, and 0 reads once; anything else, such as `.inf` or `.nan`, makes those commands exit 2 before any change. The default is 30.
- `cluster` also decides whether access statements run `ON CLUSTER`; see [Access](#access-users-roles-and-grants). They backquote its name, so a name such as `c-1` works there. It must be a string.
- `mcp_user_name` and `dict_reader_name` apply only without an `access.yaml`. With one, `bootstrap` creates neither user: declare a read-only user in `access.yaml` instead, and see [Access](#access-users-roles-and-grants) about the dictionary reader.

### Secrets

Choose `.env.local` (or exported environment variables) or per-environment SSM paths. When an SSM path is configured, it is used for that secret. A `#key` suffix extracts a JSON key from the parameter. SSM access requires AWS credentials and permission to read the specified parameters; `aws_region` is optional.

| Variable | Purpose |
|---|---|
| `CH_<ENV>_MIGRATION_PASSWORD` | Required migration password |
| `CH_<ENV>_ADMIN_PASSWORD` | Admin password for bootstrap |
| `CH_<ENV>_DICT_READER_PASSWORD` | The dictionary reader's password, for `create_dictionary` and `dict_reader_name` |
| `CH_<ENV>_MCP_PASSWORD` | Password when a read-only MCP user is configured |
| `CH_<ENV>_<NAME>` | Password of a user `access.yaml` declares with `password: {secret: NAME}`, such as `CH_DEV_DASHBOARDS_PASSWORD` for `{secret: dashboards_password}` |

A declared user's secret is read the same way: from the SSM path at `environments.<env>.ssm.<NAME>` in `config.yaml` when one is set, and otherwise from `CH_<ENV>_<NAME>`. Only `access apply` reads these secrets, and only for the passwords it sets: those of users it creates, or every declared user's with `--reset-passwords`. It reads them all before its first change, and a missing one exits 2 with nothing changed. `access validate`, `show` and `plan` never read them.

The legacy `CH_<ENV>_PASSWORD` remains supported. Never commit credentials or pass them in migration SQL that will be logged.

### Hooks

Top-level hooks run SQL on the migration connection. `pre_migrate` runs before the
migration batch; `post_migrate` runs after each revision body **before its version
is completed**, so hook mutations cannot escape the waiting barrier. A resumed
upgrade reuses its unfinished batch's receipts instead of repeating completed
hook writes. `{db}` is substituted. Hooks execute as SQLAlchemy text, not through
the SQL-file splitter; supply one statement per entry. Hook SQL is logged, so do
not put secrets in it.

```yaml
hooks:
  pre_migrate:
    - "SELECT 1"
  post_migrate:
    - "SYSTEM RELOAD DICTIONARY {db}.dict_regions"
```

Only configure the dictionary hook when that dictionary exists at every revision where the hook runs.

### Lint configuration

Non-gate rule severities can be `error`, `warn`, or `off`. Gate rules must remain `error`. `mv_validation_cutoff` can exclude older revisions from materialized-view declaration checks.

```yaml
lint:
  large_table_threshold: 100000000
  mv_validation_cutoff: "2026-01-01"
  rules:
    destructive_changes: warn
    idempotency: error
    reserved_words: warn
```

Review findings rather than treating a successful command as a guarantee that a migration is safe. DDL and mutations are not transactional.

### Bootstrap roles and Cloud notes

`bootstrap` creates the database, the migration role and the migration user, and grants the role to the user. The role is `<project>_migration_role`, or `migration_role` from `config.yaml`. What else it does depends on whether the project has an `access.yaml`:

| | With `access.yaml` | Without (legacy mode) |
|---|---|---|
| Migration role's grants | The profile it names, `core` by default, plus `migration_role.grants` | 0.8's grants: the `legacy` profile |
| Other roles and users | None. `access apply` makes what `access.yaml` declares. | `<project>_readonly_role` (`SELECT`, `SHOW TABLES`) and its user, with `mcp_user_name`; `<project>_dict_role` and its user, with `dict_reader_name` |
| Server version | 25.8 or later | 25.8 or later |
| `ON CLUSTER` | Where users and roles are on each node's own disk and `cluster` is set | Never: bootstrap each node |

Either way it grants explicit privileges, never `GRANT ALL`, and attempts every grant. Best-effort grants land as far as the admin can give them, and the rest are warnings. It ends by logging in as the migration user and checking the role's required grants. It is safe to run again, and it never revokes.

On ClickHouse Cloud the admin may not hold every best-effort grant, such as all of `SELECT ON system.*` in the `legacy` profile; each one it can't give is a warning. The `core` profile grants `SELECT` on each system table ch-migrate reads, one by one, rather than on `system.*`. Cloud grants `CLUSTER` to every login, so `CLUSTER ON *.*` is already held and nothing warns about it. Access statements don't run `ON CLUSTER` on Cloud. Its users and roles are replicated, so each read takes every replica at once, through the `default` cluster, with no wait for a row that no replica holds; the admin needs `READ ON REMOTE` for that.

Use standard table engine names such as `MergeTree` and `ReplacingMergeTree`; ClickHouse Cloud supplies its shared variants. Cloud usually uses HTTPS port `8443`; local HTTP usually uses `8123`.

## Development

Run unit tests without starting Docker:

```bash
uv run --locked pytest -q
```

Run the opt-in real-server suite:

```bash
uv run --locked pytest -q -m integration
```

The single-server fixture starts `clickhouse/clickhouse-server:26.3` in its own `chm-it-*` container on a random loopback port. Each test uses a separate database. Finalizers clean up on success, failure, and handled interrupts; a forced process kill cannot run finalizers. Docker-unavailable runs skip with a reason.

`CH_MIGRATE_IT_IMAGE` overrides the image tag. `CH_MIGRATE_IT_URL` selects a dedicated test server instead of starting Docker. It is an HTTP(S) URL with credentials supplied only through the environment. Tests create and drop databases there: never select a shared or production server, and never commit the URL.

For the single-server suite only:

```bash
uv run --locked pytest -q -m "integration and not cluster"
```

For the replicated harness:

```bash
uv run --locked --python 3.12 pytest -q -m "integration and cluster" -k harness
```

`clickhouse_cluster` owns a Docker network and two nodes, with Keeper embedded
in node 1. `it_cluster` has one shard and two replicas. `cluster_project` creates
an **Atomic database on both nodes with `ON CLUSTER`**; replicated tables use
`ReplicatedMergeTree` with a shared Keeper path and per-node replica macros.
The fixture exposes `clients[1]` and `clients[2]`, plus `stop_node(2)` and
`start_node(2)`. Read `clients[2]` again after a restart; its connection is renewed.

Tests prove row replication and both hosts' distributed-DDL completion. The
node-down scenario uses a five-second distributed-DDL timeout, inspects the
unfinished host, then proves catch-up after restart. Its HTTP response is
buffered with `wait_end_of_query=1` so the timeout is not obscured by a truncated
chunked response.

Cluster fixtures use the same image-tag override, remove their own containers,
anonymous volumes, and network, and never operate on external servers. Cluster
tests skip when `CH_MIGRATE_IT_URL` is set. The integration CI job runs cluster
tests on Python 3.12 only, and single-server tests on 3.10 and 3.14.

### Against a ClickHouse Cloud test service

Point `CH_MIGRATE_IT_URL` at a dedicated Cloud test service
(`https://<user>:<url-encoded password>@<service>:8443`), loading the password
from your secret store at run time. Use a user that can create and drop `it_*`
databases, users and roles. The fixture waits up to 300 seconds for an idle
service to wake.

```bash
uv run --locked pytest -q -m "integration and not cluster"   # the full single-server suite
uv run --locked pytest -q -m "integration and cloud"         # Cloud-only checks
```

Tests marked `cloud` run only when the URL is a Cloud service (Shared
databases); they check that a migration's `SET` reaches every later statement
whichever replica serves it, and version-table round trips on SharedMergeTree.
Cluster tests skip. Cloud grants no user `SYSTEM STOP MERGES`, so tests that
hold a mutation pending skip with that reason; the self-hosted suite still
covers them. Each test drops its own database. After an interrupted run,
`scripts/cloud_sweep.py [--dry-run]` drops `it_*` databases no query has
touched for 24 hours, with their test users and roles.

## License

MIT License — see [LICENSE](LICENSE).

## Author

Dan Young
