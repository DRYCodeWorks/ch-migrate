# clickhouse-alembic

## What it is

`ch-migrate` manages SQL-first ClickHouse migrations across environments: author SQL files, bootstrap databases and roles, inspect migrations and dependencies, and compare schema snapshots. Alembic owns revision history; the database dialect owns DDL compilation. This operational layer complements ClickHouse's official Alembic integration rather than replacing it. This development line still uses `clickhouse-sqlalchemy` for Alembic connections; it does not imply an endorsement from ClickHouse.

Background: [ClickHouse migrations with Alembic](https://www.drycodeworks.com/articles/dev-guides/clickhouse-migrations-with-alembic).

## Install

```bash
uv tool install clickhouse-alembic
# Or:
pip install clickhouse-alembic
ch-migrate --version
```

The package is `clickhouse-alembic` and the executable is `ch-migrate`: PyPI rejected `ch-migrate` because it is too similar to the existing `chmigrate` package. The Python import package remains `clickhouse_alembic`.

This README describes the source checkout, which may be ahead of PyPI. To try an unreleased checkout locally, run `uv tool install .` in the repository. For development without installing a global tool, use `uv run --locked ch-migrate`.

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

To render without executing, set `CH_ENVIRONMENT` and run `alembic upgrade head --sql`. Existing projects need `ch-migrate upgrade-env` for the offline version-table and literal-rendering fixes. The package requires Alembic 1.14 or later for that extension point.

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
from clickhouse_alembic import get_db, run_sql

def upgrade():
    run_sql("history/tables/logs/001_add_status.up.sql", db=get_db())
```

`get_db()` returns the environment database. `read_sql(path, **values)` still returns a string using Python `str.format`; unlike `run_sql`, callers must escape literal braces and execute the returned SQL themselves. For a single-statement file, the existing pattern remains valid:

```python
from alembic import op
from clickhouse_alembic import get_db, read_sql

def upgrade():
    op.execute(read_sql("history/tables/users/001_create.sql", db=get_db()))
```

A hand-written irreversible Python revision uses both the marker and backstop:

```python
from clickhouse_alembic import IrreversibleMigration

irreversible = "Drops legacy data"

def downgrade():
    raise IrreversibleMigration(revision, irreversible)
```

### Exchange and dictionary patterns

`new --exchange --table NAME` generates the existing shadow-table, copy, exchange, and drop scaffold. Coordinate or pause writers: this copy-and-swap pattern alone does not preserve inserts arriving during the copy. It is not an online-rebuild guarantee. Review the generated SQL and column mapping before applying it. The scaffold is marked irreversible because it drops the old table.

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
from clickhouse_alembic import create_dictionary

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

`ch-migrate up ENV [-r REV] [--skip-mv-check] [--verbose]` applies migrations to `head` by default, printing one line per migration. `-r/--revision` selects a target. `--skip-mv-check` bypasses materialized-view declaration validation; use it only after reviewing those findings. If a migration fails, `up` names it, the SQL file, the statement and its line, and ClickHouse's error; `--verbose` adds the Python traceback.

Example: `ch-migrate up dev --revision abc123`

### `down`

`ch-migrate down ENV [-r REV] [--verbose]` reverts one revision by default (`-1`). `-r/--revision` accepts another target. Known ranges containing irreversible revisions are refused. Failures are reported as for `up`.

Example: `ch-migrate down dev --revision base`

### `status`

`ch-migrate status ENV` shows connection information, applied/pending counts, and head status, and names the `up` command when migrations are pending. No command-specific options. Exits 1 if it cannot reach the database.

Example: `ch-migrate status dev`

### `history`

`ch-migrate history ENV` displays the revision graph and applied state. No command-specific options.

Example: `ch-migrate history dev`

### `lint`

`ch-migrate lint [ENV]` analyzes upgrade statements, not downgrade SQL. Without `ENV`, it checks every revision statically without credentials or a connection. With an environment, it checks only pending revisions and adds live size and dependency checks. If it cannot determine the pending set, it fails rather than silently checking a different scope. No command-specific options. Errors exit nonzero; warnings alone do not.

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

`ch-migrate upgrade-env` replaces `migrations/env.py` with the installed version and backs up the old file as `env.py.bak`. No command-specific options. Review and reapply local customizations from the backup.

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

Set rule severities to `error`, `warn`, or `off`. `mv_validation_cutoff` can exclude older revisions from materialized-view declaration checks.

```yaml
lint:
  large_table_threshold: 100000000
  mv_validation_cutoff: "2026-01-01"
  rules:
    destructive_changes: warn
    idempotency: warn
    reserved_words: warn
```

Review findings rather than treating a successful command as a guarantee that a migration is safe. DDL and mutations are not transactional.

### Bootstrap roles and Cloud notes

Bootstrap creates `{project}_migration_role` for schema/data operations and introspection, including explicit `system.grants` access. Optional users add `{project}_readonly_role` (SELECT/SHOW) and `{project}_dict_role` (dictionary sources). Bootstrap uses explicit grants rather than `GRANT ALL` for Cloud compatibility.

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

The fixture starts `clickhouse/clickhouse-server:26.3` in its own `chm-it-*` container on a random loopback port. Each test uses a separate database. Finalizers clean up on success, failure, and handled interrupts; a forced process kill cannot run finalizers. Docker-unavailable runs skip with a reason.

`CH_MIGRATE_IT_IMAGE` overrides the image tag. `CH_MIGRATE_IT_URL` selects a dedicated test server instead of starting Docker. It is an HTTP(S) URL with credentials supplied only through the environment. Tests create and drop databases there: never select a shared or production server, and never commit the URL.

## License

MIT License — see [LICENSE](LICENSE).

## Author

Dan Young
