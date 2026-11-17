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
Old environments are refused by `up`, `down`, and `history` before connecting;
`status` still reports.
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
| `ch-migrate bootstrap ENV [--dry-run]` | Create the database and the migration role and user |
| `ch-migrate access validate ENV [--sql]` | Check access.yaml offline; `--sql` prints SQL for a DBA |
| `ch-migrate access show ENV [NAME...]` | Print what the server holds, in access.yaml form |
| `ch-migrate access plan ENV` | Show how the server differs from access.yaml (0 in sync, 1 pending, 2 error) |
| `ch-migrate access apply ENV [--reset-passwords]` | Make the server match access.yaml, as the admin |
| `ch-migrate access check ENV` | Check the migration role's grants, as the migration user |
| `ch-migrate new ENV NAME [--table X] [--irreversible REASON]` | Create upgrade/downgrade SQL and their revision |
| `ch-migrate up ENV [-r REV]` | Apply migrations (default: head, or to REV) |
| `ch-migrate down ENV [-r REV]` | Rollback (default: last, or to REV) |
| `ch-migrate status ENV [--json]` | Show current migration state |
| `ch-migrate history ENV [--json]` | Show migration history |
| `ch-migrate lint [ENV] [--json]` | Check upgrade statements; ENV restricts to pending revisions and adds live checks |
| `ch-migrate deps ENV [--validate PATH]` | Inspect live dependencies |
| `ch-migrate snapshot ENV [--exclude GLOB] [--filter GLOB]` | Capture schema |
| `ch-migrate diff ENV [--snapshot-dir PATH] [--json]` | Compare snapshot and live schema |
| `ch-migrate rebase ENV [--onto REV] [--dry-run]` | Preview/rewrite dangling revision branches |
| `ch-migrate upgrade-env` | Refresh env.py, keeping env.py.bak |
| `ch-migrate skill [--user\|--project]` | Install Claude skill for ch-migrate |

**`--json`:** `status`, `history`, `lint` and `diff` print one document to stdout
with `schema_version` and `command`; diagnostics go to stderr. Prefer it when a
script or agent reads the result. Exit codes in JSON mode: `status` 0 at head,
1 pending or diverged, 2 unreadable; `diff` 0 in sync, 1 drift, 2 error; `history` 2 when
the database can't be read; `lint` as without `--json`. Schemas are in the
repository's `docs/schemas/`.

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
├── access.yaml           # Users, roles and grants (init writes it)
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
rules. Use `IF NOT EXISTS` / `IF EXISTS` where supported, including table,
dictionary, and database renames. Inserts, exchanges, updates/deletes,
single-part attach/detach, and cross-table partition copies/moves need a waiver.
Each finding says how to make its statement safe; follow it before reaching for
a waiver.

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
fills in `{db}` and `{password}`: the reader's password, from
`DICT_READER_PASSWORD` or else the environment's `dict_reader_password` secret
(SSM path or `CH_<ENV>_DICT_READER_PASSWORD`). Without an access.yaml it first
grants SELECT on the source table to `dict_reader`. With one it grants nothing:
declare the reader and its SELECT in access.yaml and run `access apply` before
`up`.

For offline SQL, set `CH_ENVIRONMENT` and run `alembic upgrade head --sql`.
Existing projects need `ch-migrate upgrade-env` for the offline version-table
and literal-rendering fixes. Keep credentials out of SQL files and logs.


## Roles Created by Bootstrap

`bootstrap` creates the database and the migration role and user. With an
`access.yaml` (what `init` writes), every other user and role is declared there
and made by `ch-migrate access apply ENV`, logged in as the admin. Without one,
the project is in legacy mode and gets 0.8's roles.

| Role | When | Grants |
|------|------|--------|
| `{project}_migration_role`, or `migration_role` from config.yaml | Always | With access.yaml: its profile, `core` by default, plus `migration_role.grants`. Core is schema changes (CREATE/DROP/UNDROP/ALTER TABLE, VIEW, DICTIONARY) and data ops (SELECT/INSERT/DELETE/TRUNCATE/OPTIMIZE) on `{db}.*`, SHOW, and SELECT on `system.databases`, `system.tables`, `system.mutations` and `system.dictionaries`, with no grant option and no user or role management. Without access.yaml: the `legacy` profile, 0.8's grants, which add grant option, functions, user, role and row-policy management, and `system.*` |
| `{project}_readonly_role` | Legacy mode, with `mcp_user_name` | SELECT and SHOW TABLES on `{db}.*` |
| `{project}_dict_role` | Legacy mode, with `dict_reader_name` | None at first; `create_dictionary` grants SELECT on each source table to `dict_reader` |
| Roles declared in access.yaml | `access apply` | Exactly what access.yaml declares |

Notes:
- Grants are explicit, never `GRANT ALL`. Best-effort grants (cluster,
  remote-read, sync, `system.parts`, `system.replicas`) land as far as the
  admin can give them; the rest are warnings.
- `bootstrap` and `access apply` only ever add to the migration role. Revoke
  extras by hand; `access plan` lists them with `!`.
- A core migration role can't create users or roles, or grant to another user.
  Declare them in access.yaml, or set `migration_role.profile: legacy` when
  migrations do that. A grant a migration gives to a user or role access.yaml
  declares is revoked at the next `access apply` unless access.yaml declares it
  too. With an access.yaml, `create_dictionary` grants nothing: declare the
  dictionary's reader and its SELECT there.
- The `access` commands and `bootstrap`, with or without an access.yaml, need
  ClickHouse 25.8 or later. The admin's `ROLE ADMIN` lets it grant any role to anyone.
- On a self-hosted cluster that keeps users and roles on each node's own disk,
  with `cluster` set: `access apply`, and `bootstrap` with an access.yaml, run
  ON CLUSTER (the admin needs `CLUSTER ON *.*`). Legacy `bootstrap` never does,
  so run it on each node, and run `access validate --sql` output on each node.
  `access plan`, `access apply`, and `bootstrap` with an access.yaml, read only
  the node they connect to, and name it. After a failed ON CLUSTER apply, point `host` at each node in
  turn and run `access plan` there: the first node can report in sync while
  another still lacks the change.
- Where users and roles are replicated (Keeper, or ClickHouse Cloud), the access
  commands read every replica at once through `clusterAllReplicas`, which needs
  the admin's `READ ON REMOTE`. Without it, or with no one cluster to read
  through, a missing row is read again for up to `access_read_timeout` seconds.
  A lone replicated node keeps this wait too. The reason prints once, only
  when the command actually waits.

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

# Shows each password as a placeholder, which fails to parse if run unedited:
# CREATE USER IF NOT EXISTS migration_dev IDENTIFIED BY <secret:migration_password>;

# Check access.yaml offline, then compare it with the server
ch-migrate access validate dev
ch-migrate access plan dev
```

## ClickHouse Cloud Notes

- Uses standard engines (MergeTree, ReplacingMergeTree) - auto-upgraded to Shared* on Cloud
- Default port 8443 (HTTPS), use 8123 for local HTTP
- DDL is non-transactional - migrations can't be atomically rolled back
- Cloud grants `CLUSTER` to every login, so the access commands treat core's
  best-effort `CLUSTER ON *.*` as held and don't warn about it
- Each `op.execute()` runs one statement
