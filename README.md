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

Version advancement remains **insert the new row, then delete the old row**;
there is no asynchronous version UPDATE. Online `up` journals that bookkeeping
and polls its owned DELETE rather than holding an HTTP request open with
`mutations_sync`. If interrupted, both rows can remain. A subsequent `up`
finishes the proven bookkeeping without running the revision again. Overlapping
heads without an owned journal receipt are still refused. Failed version
mutations report their reason and an operator-only KILL statement; ch-migrate
never kills them itself.

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

`up` waits for the mutations its statements create before completing a revision.
It uses the same checked ClickHouse session for submission and polling, so prior
`SET` statements stay effective. `op.execute`, raw `op.get_bind()` execution, and
`run_sql` all pass through the cursor boundary. Upgrades run in the CLI process:
killing that process cannot leave a child Alembic runner advancing revisions.

```bash
ch-migrate up dev                 # No default waiting timeout
ch-migrate up dev --timeout 120   # One waiting budget across the invocation
```

Progress names the table, mutation ID, host, remaining parts and elapsed time.
A terminal uses a live line; CI gets plain state changes and periodic summaries
on stderr. Zero remaining parts alone does not count as completion: every
required replica must report its owned mutation finished. Configure `cluster`
for self-hosted replicated tables, or use a Replicated database's automatic
cluster. Missing replicas or missing evidence are never treated as success.

An active foreign mutation ahead of ours is reported and waited before more work
is submitted. A failed predecessor stops the run. Foreign work queued after our
completion barrier is not claimed as ours and does not hold our revision open.

Ownership is persisted in `_ch_migrate_journal` **before** SQL submission. The
journal follows the version table's deployment policy and uses the same session.
It records revision/statement identities, table UUIDs and progress receipts.
Run only one migration runner per database, using a CI concurrency key or an
external lock: this journal is not a distributed lock. Do not delete or restore
it independently of the database state it describes.

After a timeout, dropped connection or killed process, ClickHouse may still be
working. Run the same `up` again: it skips proven completed statements and
reattaches to its known unfinished mutation instead of issuing it again.
Successful downgrade starts a fresh journal generation so a later upgrade
actually executes. A changed unresolved statement or replaced table is refused.
An explicitly rejected repeat-safe metadata statement can be repaired and retried;
transport errors do not establish rejection.

**Unknown outcome:** if ownership/completion evidence is missing or expired,
`up` exits nonzero, does not reissue the statement and does not complete the
migration. The error names the journal key, token and recorded UUIDs and provides
read-only inspection queries. Operator recovery is deliberately not a retry flag:

1. Stop migration runners and quiesce other writers as needed. Back up the
   affected journal key and relevant data before making changes.
2. Establish whether the original statement took effect using independent data,
   schema and server evidence on every required replica. An absent mutation row
   is not proof either way.
3. Reconcile that **specific** statement and journal key in a reviewed manual
   operation. For a proven applied operation, its completed receipt must carry
   the actual post-operation UUID map (`after_target.uuids`, or null for a dropped
   target). For a proven unapplied operation, remove only that key's attempt
   records after restoring any partial effects, and wait for that journal
   change on every replica. Preserve the other completed statement receipts.
4. Recheck the recorded heads and rerun `up`. Never blindly stamp the revision,
   delete the whole journal, or replay a non-idempotent statement to guess its
   outcome.

**Failed mutation:** `latest_fail_reason` stops the run and prints a scoped
`KILL MUTATION` statement for the operator. The tool does not execute it. Fix the
cause and reattach, or review a cancellation and reconcile its effects; killing
a mutation is not proof that it completed.

`MODIFY TTL` has a separate metadata/materialization boundary: when materialization
is enabled, the runtime journals the metadata change with automatic materialization
disabled, then submits a separately owned `MATERIALIZE TTL`. This avoids confusing
TTL's comma-separated rules with ALTER actions. Session and per-query
`materialize_ttl_after_modify = 0` are honored. Parameter batches are executed
sequentially through the same single-statement path, with a receipt and completion
barrier for each parameter set; intermediate TTL effects are not collapsed.
Lightweight DELETE keeps its row-mask semantics; lightweight UPDATE is synchronous
patch work, not a fabricated `system.mutations` record.

For `ON CLUSTER` statements, the same journal first records a unique distributed
DDL marker. `up` submits without a server-side synchronous DDL wait, then polls
`system.distributed_ddl_queue`. Every configured target host must report
`Finished` **and** exception code zero. A failed host is named with its error;
a down host remains pending until it returns or the shared `--timeout` expires.
The tool neither deletes the queue entry nor resubmits the DDL on rerun.

The queue entry is found by its recorded `log_comment`, not by matching SQL text
(the server can insert UUIDs into that text). Caller log comments are preserved
in the journal. A lost acknowledgement can reattach to the existing entry;
missing/expired queue evidence follows the unknown-outcome procedure above.
Mutation-producing distributed ALTERs must satisfy **both** barriers: DDL execution
on all hosts and completion of their owned mutations. TTL metadata and explicit
materialization retain separate queue receipts.

Single-node statements without `ON CLUSTER` do not consult that queue. Ordinary
Cloud DDL does not need `ON CLUSTER`; Cloud qualification remains separate from
the local harness. Re-run bootstrap when upgrading a restricted migration user
to obtain the available cluster and distributed-queue inspection grants.

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

## Drift detection in CI

After applying and verifying migrations in an environment, capture its expected
schema:

```bash
ch-migrate snapshot dev
ch-migrate diff dev --snapshot-dir migrations/sql/snapshots/20261002_120000 --json
```

Replace the example timestamp with the directory reported by `snapshot`. Review
and commit that directory alongside the migration head. Do not regenerate the
snapshot automatically in the drift job: doing so would accept an out-of-band
change as the new expected schema. Re-snapshot after an intentional migration or
an explicitly reviewed reconciliation, not merely to turn a failed check green.

Copy [the GitHub Actions example](docs/examples/github-actions-drift.yml) into the
consumer repository's `.github/workflows/`. It runs on a daily schedule and
same-repository pull requests; fork PRs are excluded because they have no database
secret. It does not use `pull_request_target`. All actions are pinned by commit.
The checkout must contain `config.yaml` and the committed snapshot, and the runner
must be able to reach the selected server.

Configure these repository variables:

| Variable | Value |
|---|---|
| `CH_MIGRATE_PACKAGE` | An approved, pinned `ch-migrate-cli` requirement that supports `diff --json`. There is no fallback to an older release. |
| `CH_MIGRATE_ENV` | An environment name in `config.yaml`, such as `dev`. |
| `CH_MIGRATE_SNAPSHOT_DIR` | That environment's committed snapshot directory. |

Set the repository secret `DRIFT_MIGRATION_PASSWORD` to the password for that
environment's configured user. Prefer a dedicated schema-inspection account,
not an admin account. The job writes a private `.env.local` without printing the
value and removes the file even when comparison fails.

`diff --json` exits **0** for matching schemas, **1** for drift, and **2** for a
configuration/connection error. The workflow saves `drift.json`, uploads it even
on comparison failure, then fails the job for either nonzero result. The report
names changed tables and fields. The example's install and shell steps are tested
locally against the source checkout; that does not publish the package or prove
that a previously released version contains JSON support.

Without `--snapshot-dir`, `diff` sorts entries in `migrations/sql/snapshots/` by
name and chooses the last one. It does not select a snapshot by environment or
recorded migration revision. Use an explicit path in CI, especially when one
repository holds snapshots for several environments.

## Statement classification

`ch_migrate.classify.classify(statement, live_schema=None)` accepts SQL or
an extracted migration statement. It returns `Classification(kind, table, detail)`;
`live_schema` is the existing introspection `Schema`. Classification does not run
SQL or change lint's gate rules.

- `metadata`: no background mutation, such as ADD COLUMN or a default-only change.
- `mutation`: a background mutation of parts, including DROP INDEX/PROJECTION and
  RENAME COLUMN, not just UPDATE/DELETE.
- `rebuild`: the requested key or engine change cannot be made by that in-place ALTER.
- `other`: non-schema operations or work not covered by the classifier.

Without a live column type, MODIFY COLUMN is conservatively a mutation **if the
type changes**. MODIFY TTL materializes existing parts unless the statement sets
`materialize_ttl_after_modify = 0`. Lightweight DELETE creates a mutation;
lightweight UPDATE writes synchronous patch parts and is explicitly `other`,
not a nonexistent background mutation. A sorting-key extension is metadata only
when it preserves the old key and appends columns added by the same ALTER.

The [classification corpus](tests/corpus/classification.yaml) is checked against
fresh seeded MergeTree tables: every ALTER must create the predicted mutation,
create none, or be rejected as an unsupported in-place change. The server proof,
not the apparent SQL verb, defines the classification.

## Plan pending migrations

Run `ch-migrate plan dev` before `up`. It reads pending upgrade statements in
apply order, classifies them against the live schema, reports rewrite bytes and
dependent materialized views/dictionaries, and includes every lint finding.
It does not import revision modules, execute SQL from migrations, or run
`SYSTEM SYNC`. Python-generated SQL that cannot be extracted statically is not
included. The result describes current live state, not a simulation of each
preceding pending statement.

```bash
ch-migrate plan dev
ch-migrate plan dev --json
```

For example, these two pending statements produce different size labels:

```sql
ALTER TABLE analytics.events MODIFY COLUMN value UInt32;
-- ch-migrate: allow-non-idempotent reviewed one-time correction
ALTER TABLE analytics.events UPDATE value = value + 1 WHERE id = 1;
```

- The column change reports **exact** compressed/uncompressed column bytes and
  distinct active parts from `system.parts_columns` when column sizes are available.
- The UPDATE reports **ceiling: up to** the active table's compressed/uncompressed
  bytes and part count. An explicit partition ID or a simple equality on
  `_partition_id`, an unsigned identity partition key, or a supported date-bucket
  partition expression narrows the ceiling. Other predicates remain table-wide.
- Compact parts share a data file and report zero per-column byte counters.
  These use a whole-part **ceiling**, not an exact zero-byte claim.
- The waiver appears with its reason. It removes the UPDATE's gate blocker, not
  its size or dependency warnings.

Rebuild-required statements also run shared read-only preflight. It reports
Distributed/sharded layouts, changed partition keys, unfinished mutations,
incompatible physical-transfer definitions, and unacknowledged async writers.
Storage and part counts use the maximum across inspected replicas, not the sum
of replica copies. Disk-space and partition-part warnings identify each host.
The insert rate comes from completed initial INSERTs in the last 60 minutes.

Writer inspection includes rotated `query_log_N` tables and inherited
user/profile/role settings, with a bounded log scan. Missing privileges, absent
logs, or a scan exceeding its bound are errors, not evidence of safe writers.
Unknown effective settings refuse a rebuild. A quiet or unflushed log does not
prove that no writers exist. The shared `RebuildRequest` API accepts
`allow_unacknowledged_async_loss=True` only for an explicitly reviewed
migration-file opt-in; `plan` has no command-line bypass.

Only ReplacingMergeTree variants can collapse identical sorting-key copies on
merge. Other targets may retain duplicates, including Collapsing and
VersionedCollapsing tables with identical positive rows.

The [plan JSON schema](docs/schemas/plan.schema.json) covers both success and
error documents. Human and JSON output use the same facts. Exit codes are **0**
for a successful inspection, including warnings/preflight refusals; **1** when
the idempotency/standalone-SET gate would refuse `up`; and **2** for an inspection
error. Preflight refusals remain explicit in each statement's rebuild findings.
Read-only plans do not synchronize replicas or protect against concurrent changes.

## Command reference

Every command accepts `--help`. Top-level `ch-migrate --version` reports the installed package version. `ENV` below names an entry in `config.yaml`.

Output lines start with `→` for a step, `✓` for a result, `!` for a warning and `✗` for an error; warnings and errors go to stderr. Colour is dropped when output is not a terminal or `NO_COLOR` is set, and lines are never wrapped, so paths and SQL can be copied or grepped.

`status`, `history`, `plan`, `lint`, and `diff` accept `--json`. JSON mode writes one document to stdout; diagnostics go to stderr. Every document includes `"schema_version": 1` and `"command"`. From 1.0 this is a public interface: breaking changes require a major version. Errors include an `error` string rather than inventing a successful empty result. The schemas linked below use JSON Schema draft 2020-12.

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

### `plan`

`ch-migrate plan ENV [--json]` inspects pending upgrades without executing them.
See [Plan pending migrations](#plan-pending-migrations) for byte precision,
rebuild checks, limitations, exit codes, and an annotated example.

### `lint`

`ch-migrate lint [ENV] [--json]` analyzes upgrade statements, not downgrade SQL. Without `ENV`, it checks revisions after the gate baseline statically without credentials or a connection. With an environment, it checks only pending revisions in that scope and adds live dependency checks. Use `plan` for rewrite sizes. If it cannot determine the pending set, it fails rather than silently checking a different scope. Errors exit nonzero; warnings and waiver INFO lines alone do not.

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
  mv_validation_cutoff: "2026-01-01"
  rules:
    destructive_changes: warn
    idempotency: error
    reserved_words: warn
```

Review findings rather than treating a successful command as a guarantee that a migration is safe. DDL and mutations are not transactional.

`large_table_mutation` and `large_table_threshold` are retired. Existing entries
are ignored with one deprecation warning pointing to `ch-migrate plan`.

### Bootstrap roles and Cloud notes

Bootstrap creates `{project}_migration_role` for schema/data operations and introspection, including explicit `system.grants`, `system.databases`, `system.tables`, and `system.mutations` access. It grants available cluster, remote-read, and synchronization rights through `CURRENT GRANTS`. Optional users add `{project}_readonly_role` (SELECT/SHOW) and `{project}_dict_role` (dictionary sources). Bootstrap uses explicit grants rather than `GRANT ALL` for Cloud compatibility.

Plan also needs readable `system.parts`, `system.parts_columns`, and, for rebuild
preflight, disks, MergeTree settings, user/profile/role metadata, and query logs.
Bootstrap grants these where the admin's current grants permit. Existing
deployments may need to rerun bootstrap or grant missing access explicitly,
including readable rotated query logs. A denied inspection fails rather than
silently omitting evidence.

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
