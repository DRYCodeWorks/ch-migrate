# ch-migrate Quick Reference

The commands and setup steps on one page. The [README](../README.md) is the full guide;
its [Quick start](../README.md#quick-start) walks through a first migration step by step.

## New project

```bash
# 1. Install
uv add ch-migrate-cli                 # or: pip install ch-migrate-cli

# 2. Initialize: writes config.yaml, access.yaml and migrations/
ch-migrate init --name my_project
ch-migrate init ./migrations --name my_project    # or in a subdirectory: then `cd migrations`,
                                                  # since every command runs from there

# 3. Configure
#    config.yaml: the server, database and migration_user for each environment
#    .env.local:  CH_DEV_ADMIN_PASSWORD and CH_DEV_MIGRATION_PASSWORD

# 4. Bootstrap: creates the database, the migration role and the migration user
ch-migrate bootstrap dev --dry-run    # print the SQL; reads no secrets
ch-migrate bootstrap dev

# 5. Create a migration: an .up.sql and a .down.sql under migrations/sql/history/
ch-migrate new dev add_status --table logs

# 6. Apply it
ch-migrate up dev

# 7. Users and roles: declare them in access.yaml, then
ch-migrate access plan dev            # preview
ch-migrate access apply dev           # make the server match, as the admin
```

The migration role gets the `core` profile: what migrations need on the project's
database, with no right to manage users or roles. `bootstrap` and the `access` commands
that connect need ClickHouse 25.8 or later.

To set up a fresh environment: `bootstrap`, `up`, then `access apply`. To deploy: `up`,
then `access apply`. With dictionaries whose reader `access.yaml` declares, run `access apply`
before `up`.

## Existing project

### Adding ch-migrate to an existing database

```bash
uv add ch-migrate-cli
ch-migrate init ./migrations --name my_project
cd migrations                     # every command runs from the project directory
# Edit config.yaml and create .env.local
ch-migrate bootstrap dev
```

`bootstrap` only adds to the migration role and creates the database only if it is
missing. Existing tables are left alone. Write new migrations with
`CREATE TABLE IF NOT EXISTS` and `ADD COLUMN IF NOT EXISTS`, so a migration that meets
an object that already exists still succeeds. See
[Re-runnable migrations](../README.md#re-runnable-migrations).

### Upgrading from 0.8

A project without an `access.yaml` keeps 0.8's grants. `bootstrap` now names each grant
that fails and ends by checking the migration role's grants as the migration user. Any
command's error before a change now exits 2 instead of 1. Running `init` again adds an
`access.yaml`, which switches the next `bootstrap` to the `core` profile; delete the file
to keep 0.8's behavior. Details: [Upgrade from 0.x](../README.md#upgrade-from-0x).

### Adopting access.yaml where users and roles already exist

1. If your migrations create users or roles, or grant to other users, set
   `migration_role.profile: legacy` in `access.yaml`. That keeps 0.8's grants.
2. Start each existing user or role from what the server holds:
   `ch-migrate access show dev NAME > access.show.yaml` writes it in `access.yaml` form.
   Copy what you keep into `access.yaml`. Never redirect into `access.yaml` itself: the
   shell empties it first.
3. Review `ch-migrate access plan dev` before the first `access apply`. `apply` revokes,
   from each user and role the file declares, every grant the file leaves out. Users and
   roles the file doesn't declare are never touched.

## Commands

| Command | What it does |
|---------|--------------|
| `ch-migrate status dev` | Show which revisions are applied |
| `ch-migrate new dev <name> [--table T \| --view V \| --dict D]` | Create a revision with its `.up.sql` and `.down.sql` |
| `ch-migrate up dev` | Apply pending migrations, waiting for the mutations and `ON CLUSTER` DDL they start |
| `ch-migrate down dev` | Roll back the last migration |
| `ch-migrate down dev -r <rev>` | Roll back to a revision |
| `ch-migrate history dev` | Show migration history |
| `ch-migrate lint [dev]` | Lint upgrade statements; without an environment, offline |
| `ch-migrate snapshot dev` | Save the live schema as a snapshot |
| `ch-migrate diff dev` | Compare the live schema with the latest snapshot; exits 1 on drift |
| `ch-migrate access validate dev [--sql]` | Check `access.yaml` offline; `--sql` prints the SQL for a DBA to run |
| `ch-migrate access show dev [NAME...]` | Print what the server holds, in `access.yaml` form |
| `ch-migrate access plan dev` | List the changes `apply` would make; exits 0 in sync, 1 with changes pending |
| `ch-migrate access apply dev` | Make the server match `access.yaml`, as the admin |
| `ch-migrate access check dev` | Check, as the migration user, that it holds the migration role's grants |

`bootstrap`, `access show`, `access plan` and `access apply` log in as `admin_user`.
`access check` is for servers where ch-migrate has no admin login. An error before any
change exits 2.

## Troubleshooting

```bash
# Can the migration user log in?
ch-migrate status dev

# Does the admin login work, and what would apply change?
ch-migrate access plan dev

# Does the migration user hold its grants?
ch-migrate access check dev

# What SQL would bootstrap run? Passwords print as <secret:NAME> placeholders.
ch-migrate bootstrap dev --dry-run

# Query ClickHouse directly
curl "http://localhost:8123/?user=default&password=xxx" -d "SHOW DATABASES"
```

## Secrets

Each secret comes from AWS SSM when `config.yaml` gives a path for it under the
environment's `ssm:`, and otherwise from an environment variable, which `.env.local` can
set.

| Variable | Needed for |
|----------|------------|
| `CH_<ENV>_MIGRATION_PASSWORD` | Every command that connects |
| `CH_<ENV>_ADMIN_PASSWORD` | `bootstrap`, `access show`, `access plan`, `access apply` |
| `CH_<ENV>_<NAME>` | Each `{secret: NAME}` in `access.yaml`: `{secret: etl_password}` reads `CH_DEV_ETL_PASSWORD` |
| `CH_<ENV>_MCP_PASSWORD` | Only without an `access.yaml`, with `mcp_user_name` set |

```bash
# .env.local: gitignored, local secrets only
CH_DEV_ADMIN_PASSWORD=local-admin-pass
CH_DEV_MIGRATION_PASSWORD=local-migration-pass
```

```yaml
# config.yaml: production secrets from SSM
environments:
  production:
    host: prod.clickhouse.cloud
    database: my_project
    migration_user: migration_prod
    ssm:
      admin_password: /my_project/prod/admin_password
      migration_password: /my_project/prod/migration_password
      etl_password: /my_project/prod/etl_password    # {secret: etl_password}
```

SSM support (`boto3`) is in the base install. AWS credentials come from your usual AWS
profile or environment.

## A read-only user

For BI tools, dashboards and MCP servers. Declare it in `access.yaml`:

```yaml
roles:
  readonly:
    grants:
      - SELECT, SHOW TABLES ON {db}.*

users:
  mcp_reader:
    password: {secret: mcp_reader_password}    # CH_DEV_MCP_READER_PASSWORD
    roles: [readonly]
```

Then run `ch-migrate access plan dev` and `ch-migrate access apply dev`.

A project without an `access.yaml` can still set `mcp_user_name` in `config.yaml`, as in
0.8. `bootstrap` then creates `<project>_readonly_role` and that user.

## Local ClickHouse in Docker

```bash
docker run -d --name clickhouse -p 8123:8123 \
  -e CLICKHOUSE_PASSWORD=admin123 \
  -e CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1 \
  clickhouse/clickhouse-server:latest
```

`CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1` lets the `default` user create users and roles,
which `bootstrap` and `access apply` need.

```yaml
# config.yaml
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

```bash
# .env.local
CH_DEV_ADMIN_PASSWORD=admin123
CH_DEV_MIGRATION_PASSWORD=migpass123
```

Then `ch-migrate bootstrap dev`, `ch-migrate new dev ...` and `ch-migrate up dev`.

## More

- [Python migrations](../README.md#python-migrations), for logic SQL files can't express
- [Exchange and dictionary patterns](../README.md#exchange-and-dictionary-patterns)
- [Irreversible migrations](../README.md#irreversible-migrations)
- [Access: users, roles and grants](../README.md#access-users-roles-and-grants)
- [Drift detection in CI](../README.md#drift-detection-in-ci)
