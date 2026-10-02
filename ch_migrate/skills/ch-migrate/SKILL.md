---
name: ch-migrate
description: Use when integrating ClickHouse migrations into a project, setting up ch-migrate, creating migration files, bootstrapping ClickHouse databases, or troubleshooting ClickHouse Alembic issues. Triggers on "ClickHouse migration", "ch-migrate", "Alembic ClickHouse", "bootstrap ClickHouse", "migration user", "EXCHANGE TABLES".
---

# ch-migrate: ClickHouse Migration Tool

## Overview

`ch-migrate` adds SQL-first authoring, environment configuration, bootstrap,
inspection, and drift checks above Alembic and ClickHouse's official
`clickhouse-connect[alembic]` integration. Do not imply an endorsement or promise
transactional DDL.

**Install:** `uv tool install ch-migrate-cli` or `pip install ch-migrate-cli`.
The command is `ch-migrate`; the import package is `ch_migrate`.

From 0.6, Python 3.10+ is required; 0.5.x is the last line for 3.9. Existing
projects must run `ch-migrate upgrade-env` to install the version-2 shim.
The old environment is backed up, and repeating the command preserves that backup.
It also records current script heads as `lint.gate_baseline`, preserving YAML
comments. Review that committed boundary; it exempts those revisions and their
ancestors in every environment. Repeating `upgrade-env` updates the boundary.
Old environments are refused by `up`, `down`, `status`, and `history` before connecting.
One checked HTTP session spans the whole run: `SET` carries into subsequent
revisions. Prefer statement-level `SETTINGS` for query-local changes. An expired
session fails loudly. Configure idle `session_timeout` in seconds (default 1800).
Static lint rejects standalone `SET` on environments without the v2 marker.
Upgrade the environment or use a statement-level `SETTINGS` clause instead.
The `standalone_set` gate rule cannot be lowered through configuration.

## CLI Quick Reference

| Command | Description |
|---------|-------------|
| `ch-migrate init [PATH] [--name NAME]` | Initialize project structure |
| `ch-migrate bootstrap ENV [--dry-run]` | Create database, roles, users |
| `ch-migrate new ENV NAME [--table X] [--irreversible REASON]` | Create upgrade/downgrade SQL and their revision |
| `ch-migrate up ENV [-r REV]` | Apply migrations (default: head, or to REV) |
| `ch-migrate down ENV [-r REV]` | Rollback (default: last, or to REV) |
| `ch-migrate status ENV` | Show current migration state |
| `ch-migrate history ENV` | Show migration history |
| `ch-migrate lint [ENV]` | Check upgrade statements; ENV restricts to pending revisions and adds live checks |
| `ch-migrate deps ENV [--validate PATH]` | Inspect live dependencies |
| `ch-migrate snapshot ENV [--exclude GLOB] [--filter GLOB]` | Capture schema |
| `ch-migrate diff ENV [--snapshot-dir PATH]` | Compare snapshot and live schema |
| `ch-migrate rebase ENV [--onto REV] [--dry-run]` | Preview/rewrite dangling revision branches |
| `ch-migrate upgrade-env` | Refresh env.py, keeping env.py.bak |
| `ch-migrate skill [--user\|--project]` | Install Claude skill for ch-migrate |

**Options for `new`:** choose one of `--table NAME`, `--view NAME`, and
`--dict NAME`. SQL lives under `migrations/sql/history/{tables|views|dictionaries}/NAME/`;
without an object it goes under `history/other/`. Filenames are
`<YYYY_MM_DD_HHMM>_<revision>_<slug>.up.sql` and `.down.sql`.
`--irreversible REASON` omits the down file and installs a static marker plus
`IrreversibleMigration` backstop. `--python` retains the Python template and
optional single SQL file. `--exchange --table NAME` retains the exchange
scaffold. These three modes are mutually exclusive; invalid choices fail
before Alembic writes a revision.

## Project Structure

```
project/
├── config.yaml           # ClickHouse hosts and settings
├── .env.local            # Secrets (gitignored)
├── alembic.ini           # Alembic configuration
└── migrations/
    ├── env.py            # Alembic environment
    ├── versions/         # Generated revision adapters; no Python edits needed
    └── sql/
        ├── bootstrap/    # Custom bootstrap SQL (optional)
        └── history/      # Object-centric SQL versions
            ├── tables/
            ├── views/
            ├── dictionaries/
            └── other/
```

## Configuration

### config.yaml

```yaml
project:
  name: my_project

defaults:
  port: 8443                    # 8123 for local HTTP
  secure: true                  # false for local Docker
  admin_user: default
  # Optional users (uncomment to enable):
  # mcp_user_name: mcp_reader   # Read-only for AI tools
  # dict_reader_name: dict_reader

environments:
  dev:
    host: dev.clickhouse.cloud  # or localhost for Docker
    database: my_project_dev
    migration_user: migration_dev
    # aws_region: us-east-1  # Optional: for region-scoped SSM lookups
    # Optional SSM paths (if set, fetches from SSM directly):
    # Supports JSON key extraction: /path/to/param#json_key
    # ssm:
    #   admin_password: /my_project/dev/admin_password
    #   migration_password: /my_project/credentials#password

  staging:
    host: staging.clickhouse.cloud
    database: my_project_staging
    migration_user: migration_staging

  production:
    host: prod.clickhouse.cloud
    database: my_project
    migration_user: migration_prod
```

### .env.local

```bash
# Required
CH_DEV_MIGRATION_PASSWORD=your-migration-password
CH_DEV_ADMIN_PASSWORD=your-admin-password    # For bootstrap only

# Optional (if mcp_user_name configured)
CH_DEV_MCP_PASSWORD=your-mcp-password

# Repeat for staging/production with appropriate env name
CH_PRODUCTION_MIGRATION_PASSWORD=prod-password
CH_PRODUCTION_ADMIN_PASSWORD=prod-admin-password
```

## Integration Workflow

### SQL-first workflow

Use a dedicated, authorized server. Local HTTP normally uses port `8123` and
`secure: false`; Cloud normally uses HTTPS `8443`. Do not start or modify shared
infrastructure as part of trying the tool.

```bash
ch-migrate init ./schema --name my_project
cd schema
# Edit config.yaml and create .env.local here.
ch-migrate bootstrap dev --dry-run
ch-migrate bootstrap dev
ch-migrate new dev add_status --table logs
```

Fill the generated `.up.sql` with:

```sql
CREATE TABLE IF NOT EXISTS {db}.logs (id UInt64)
ENGINE = MergeTree ORDER BY id;
ALTER TABLE {db}.logs ADD COLUMN IF NOT EXISTS status String;
```

For an empty test project only, fill `.down.sql` with:

```sql
DROP TABLE IF EXISTS {db}.logs;
```

Then run:

```bash
ch-migrate lint
ch-migrate up dev
ch-migrate status dev
ch-migrate history dev
ch-migrate down dev
```

Do not edit the generated Python adapter. Empty/comment-only SQL files fail.
The example downgrade drops the table and its data; do not apply it to a live
table that needs preserving.

### SQL execution

`run_sql` splits on semicolons outside strings, identifiers, comments, and
heredocs, then sends one statement per request. It substitutes `{db}`,
`{cluster}`, and `{on_cluster}`, plus explicit keyword overrides. Other braces
remain literal, including JSON and `{id:UInt64}` parameters. Doubled braces
are not escapes. A failure stops later statements but cannot undo earlier DDL.

For migrations requiring logic:

```bash
ch-migrate new dev backfill --python
```

`read_sql` and `get_db` remain available to Python migrations. `read_sql` still
uses `str.format`, so its literal-brace rules differ from `run_sql`.

### Re-runnable migrations

Before Alembic runs, `up` refuses pending statements that violate idempotency
rules. Use `IF NOT EXISTS` / `IF EXISTS` where supported. Inserts, exchanges,
renames, updates/deletes, and cross-table partition copies/moves need a waiver.

```sql
-- ch-migrate: allow-non-idempotent Reviewed backfill into a deduplicating destination
INSERT INTO {db}.target SELECT id FROM {db}.source;
```

The reason is mandatory and the comment must be directly above its statement.
For inline `op.execute`, use a Python `#` comment. Waivers remain visible as INFO;
they do not make an operation idempotent. There is no CLI bypass, and lowering
gate-rule severity in `lint.rules` is an error. `--skip-mv-check` only skips
nonblocking MV checks. Other findings warn in `up`.

Recovery is to fix the cause and run `up` again. Review earlier effects before
retrying waived operations. `init` writes no baseline; upgraded projects exempt
the baseline and ancestors, even where those revisions are still pending.
`lint` checks after the baseline; `lint ENV` also restricts to pending revisions.
Exchange scaffolds require explicit reviewed waivers; none are generated for you.

### Irreversible changes

```bash
ch-migrate new dev drop_legacy --table logs --irreversible "Drops legacy data"
```

`down` statically checks the whole known range before running Alembic and refuses
if any revision carries an irreversible marker. It understands `-N` on a linear
chain, full or unique-prefix IDs, and `base`. Unknown ranges fall back to each
migration's exception; direct Alembic calls also rely on that backstop.

There is no override flag. Implement a real downgrade and remove the marker
through review to revert past it. Hand-written Python revisions must provide
both `irreversible = "reason"` and a downgrade that raises
`IrreversibleMigration(revision, irreversible)`.

### Exchange and dictionary operations

`new --exchange --table NAME` creates the existing copy-and-swap scaffold.
Pause or coordinate writers: copying and exchanging alone does not preserve
inserts arriving during the copy. Review the schema and column mapping.
The generated revision is irreversible because it drops the old table.

For dictionaries, `create_dictionary("history/dictionaries/NAME/file.sql")`
retains the configured dictionary reader's automatic SELECT grant.

For offline SQL, set `CH_ENVIRONMENT` and run `alembic upgrade head --sql`.
Existing projects need `ch-migrate upgrade-env` for the offline version-table
and literal-rendering fixes. Keep credentials out of SQL files and logs.


## Roles Created by Bootstrap

| Role | Purpose |
|------|---------|
| `{project}_migration_role` | Schema changes (CREATE/DROP/ALTER TABLE/VIEW/DICTIONARY), data ops (SELECT/INSERT/DELETE/TRUNCATE), and introspection including explicit `system.grants` access |
| `{project}_readonly_role` | SELECT + SHOW (if mcp_user configured) |
| `{project}_dict_role` | Dictionary source access (if dict_reader configured) |

Note: Bootstrap uses explicit grants (not `GRANT ALL`) for ClickHouse Cloud compatibility.

## Troubleshooting

| Issue | Solution |
|-------|----------|
| "migration_user required" | Add `migration_user: username` to environment in config.yaml |
| "Connection refused" | Check host/port. Local Docker: port 8123, secure: false |
| Passwords not loading | Ensure .env.local exists in project root (not migrations/) |
| Bootstrap hangs | Verify admin_password is correct for admin_user |
| "Database does not exist" | Run `ch-migrate bootstrap ENV` first |

### Verify Configuration

```bash
# Check what SQL would run
ch-migrate bootstrap dev --dry-run

# Should show masked passwords like:
# CREATE USER IF NOT EXISTS migration_dev
# IDENTIFIED BY '********';
```

## ClickHouse Cloud Notes

- Uses standard engines (MergeTree, ReplacingMergeTree) - auto-upgraded to Shared* on Cloud
- Default port 8443 (HTTPS), use 8123 for local HTTP
- DDL is non-transactional - migrations can't be atomically rolled back
- Each `op.execute()` runs one statement
