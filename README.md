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

0.6 requires Python 3.10+; 0.5.x is the last line supporting Python 3.9.
After upgrading the package, run this in each migration project:

```bash
ch-migrate upgrade-env
```

`init` and `upgrade-env` write a thin version-2 environment that delegates to the
package. The old file is saved as `migrations/env.py.bak`; repeating the command
on the current shim leaves that backup intact. Review any old customizations.
Existing Python migrations and version tables continue to work without edits.
`up`, `down`, `status`, and `history` refuse old environments before connecting
and tell you to run `upgrade-env`.

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

## Concepts

### Project layout

```text
project/
├── config.yaml
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

### Re-runnable migrations

`up` statically checks pending upgrade statements before Alembic executes any of
them. Idempotency and standalone-SET errors refuse the whole run and print the source file, line,
statement, and suggested fix. There is no command-line bypass; `--skip-mv-check`
does not skip this gate. Other findings are warnings in `up`; standalone `lint`
retains their configured severities.

Static lint rejects a standalone `SET` when `migrations/env.py` lacks the
`CH_MIGRATE_ENV_VERSION = 2` marker: that statement would be ignored on the old
connection. Run `ch-migrate upgrade-env`, or put the setting in the relevant
statement's `SETTINGS` clause. Version-2 projects allow `SET`; `SETTINGS` clauses
and the word `SET` inside string literals are not flagged. `standalone_set` is
a gate rule whose severity cannot be lowered.

Use `IF NOT EXISTS` for `CREATE` and `ALTER ... ADD`, and `IF EXISTS` for `DROP`,
`ALTER ... DROP`, and `RENAME COLUMN`, where supported. This includes tables,
views, materialized views, dictionaries, databases, users, roles, row policies,
settings profiles, quotas, functions, and named collections. Column, index,
projection, and constraint changes are checked separately. `CREATE OR REPLACE`
is accepted. `ATTACH` and `DETACH` need their matching `IF` form.

Inserts, exchanges, table/dictionary/database renames, updates/deletes,
`ATTACH PARTITION ... FROM`, and `MOVE PARTITION ... TO TABLE` need a reasoned
waiver. Put it directly above the statement, without a separating blank line:

```sql
-- ch-migrate: allow-non-idempotent This backfill targets a reviewed deduplicating table
INSERT INTO {db}.target SELECT id FROM {db}.source;
```

For inline Python SQL, use the same directive in a `#` comment directly above
`op.execute`. Empty reasons remain errors. Waivers print as INFO with their
reasons in `lint` and `up`; they do not make the statement idempotent.
`MODIFY`, `MATERIALIZE`, `CLEAR COLUMN`, `TRUNCATE`, `OPTIMIZE`, `SYSTEM`, grants,
revokes, `REPLACE PARTITION`, and comments are not flagged by this rule.
The reviewable classification corpus is `tests/corpus/idempotency.yaml`.

**Recovery:** fix the cause of a partial failure, then run `ch-migrate up ENV`
again. Earlier idempotent statements can run again; the version advances only
when the revision completes. Before retrying waived operations, review what the
earlier attempt actually changed.

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

The dictionary helper retains automatic SELECT grants for a configured dictionary reader:

```python
from ch_migrate import create_dictionary

def upgrade():
    create_dictionary("history/dictionaries/dict_users/001_create.sql")
```

## Command reference

Every command accepts `--help`. Top-level `ch-migrate --version` reports the installed package version. `ENV` below names an entry in `config.yaml`.

Output lines start with `→` for a step, `✓` for a result, `!` for a warning and `✗` for an error; warnings and errors go to stderr. Colour is dropped when output is not a terminal or `NO_COLOR` is set, and lines are never wrapped, so paths and SQL can be copied or grepped.

### `init`

`ch-migrate init [PATH] [-n NAME]` initializes the current directory by default. `-n/--name` sets the project name; otherwise it uses the directory name.

Example: `ch-migrate init analytics --name analytics`

### `bootstrap`

`ch-migrate bootstrap ENV [--dry-run] [-v]` creates the database, roles, and configured users. `--dry-run` prints SQL without executing it; `-v/--verbose` prints statements during execution. Requires admin and migration credentials.

Example: `ch-migrate bootstrap dev --dry-run`

### `new`

`ch-migrate new ENV NAME [--table T | --view V | --dict D] [--irreversible REASON | --python | --exchange]` creates SQL-first migrations by default. Object-option aliases are `-t`, `-v`, and `-d`. `--python` keeps the Python template. `--exchange` requires `--table`. The three authoring-mode options are mutually exclusive, and conflicts fail before a revision is written.

Example: `ch-migrate new dev add_status --table logs`

### `up`

`ch-migrate up ENV [-r REV] [--skip-mv-check] [--verbose]` applies migrations to `head` by default after the idempotency gate passes, printing one line per migration. `-r/--revision` selects a target. `--skip-mv-check` skips nonblocking materialized-view declaration checks, not the idempotency gate; use it only after reviewing those findings. If a migration fails, `up` names it, the SQL file, the statement and its line, and ClickHouse's error; `--verbose` adds the Python traceback.

Example: `ch-migrate up dev --revision abc123`

### `down`

`ch-migrate down ENV [-r REV] [--verbose]` reverts one revision by default (`-1`). `-r/--revision` accepts another target. Known ranges containing irreversible revisions are refused. Failures are reported as for `up`.

Example: `ch-migrate down dev --revision base`

### `status`

`ch-migrate status ENV` shows connection information, applied/pending counts, and head status, and names the `up` command when migrations are pending. No command-specific options. Status is a report: it exits 0 when the database is unreachable (with a warning) or migrations are pending, and exits 1 only when the configuration or `migrations/versions/` is missing, so CI can run it as a non-blocking check.

Example: `ch-migrate status dev`

### `history`

`ch-migrate history ENV` displays the revision graph and applied state. No command-specific options.

Example: `ch-migrate history dev`

### `lint`

`ch-migrate lint [ENV]` analyzes upgrade statements, not downgrade SQL. Without `ENV`, it checks revisions after the gate baseline statically without credentials or a connection. With an environment, it checks only pending revisions in that scope and adds live size and dependency checks. If it cannot determine the pending set, it fails rather than silently checking a different scope. No command-specific options. Errors exit nonzero; warnings and waiver INFO lines alone do not.

Example: `ch-migrate lint`

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

`ch-migrate diff ENV [-s PATH]` compares the live schema with the latest snapshot. `-s/--snapshot-dir PATH` chooses another snapshot. Exit code 0 means no drift; 1 means drift or an execution error.

Example: `ch-migrate diff dev --snapshot-dir migrations/sql/snapshots/20261002_120000`

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
```

### Secrets

Choose `.env.local` (or exported environment variables) or per-environment SSM paths. When an SSM path is configured, it is used for that secret. A `#key` suffix extracts a JSON key from the parameter. SSM access requires AWS credentials and permission to read the specified parameters; `aws_region` is optional.

| Variable | Purpose |
|---|---|
| `CH_<ENV>_MIGRATION_PASSWORD` | Required migration password |
| `CH_<ENV>_ADMIN_PASSWORD` | Admin password for bootstrap |
| `CH_<ENV>_DICT_READER_PASSWORD` | Password when a dictionary reader is configured |
| `CH_<ENV>_MCP_PASSWORD` | Password when a read-only MCP user is configured |

The legacy `CH_<ENV>_PASSWORD` remains supported. Never commit credentials or pass them in migration SQL that will be logged.

### Hooks

Top-level hooks run SQL on the migration connection. `pre_migrate` runs before the migration batch; `post_migrate` runs after each revision. `{db}` is substituted. Hooks execute as SQLAlchemy text, not through the SQL-file splitter; supply one statement per entry. Hook SQL is logged, so do not put secrets in it.

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

Bootstrap creates `{project}_migration_role` for schema/data operations and introspection, including explicit `system.grants`, `system.databases`, `system.tables`, and `system.mutations` access. It grants available cluster, remote-read, and synchronization rights through `CURRENT GRANTS`. Optional users add `{project}_readonly_role` (SELECT/SHOW) and `{project}_dict_role` (dictionary sources). Bootstrap uses explicit grants rather than `GRANT ALL` for Cloud compatibility.

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

## License

MIT License — see [LICENSE](LICENSE).

## Author

Dan Young
