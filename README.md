# clickhouse-alembic

Alembic-based migrations for ClickHouse, optimized for ClickHouse Cloud.

## What is ch-migrate?

`ch-migrate` is a CLI tool for managing ClickHouse schema migrations. It:
- Initializes project structure with config files
- Bootstraps databases with proper roles and users
- Runs Alembic migrations against ClickHouse

## Features

- **Multi-environment support** - Manage dev, staging, and production with separate configs
- **ClickHouse Cloud compatible** - Uses standard engines (auto-upgraded to Shared* on Cloud), handles non-transactional DDL
- **Role-based access control** - Creates roles for migration, read-only (MCP), and dictionary access
- **Object-centric SQL history** - Track all versions of each table/view/dictionary
- **Zero-downtime migrations** - `EXCHANGE TABLES` pattern for safe schema changes
- **Bootstrap command** - One-command database and user setup (idempotent)
- **SSM support** - Store credentials in AWS SSM Parameter Store
- **YAML + env vars** - Config in version control, secrets in environment

## Quick Start

### Installation

The package on PyPI is `clickhouse-alembic`; the command it installs is `ch-migrate`.

**For CLI usage** (recommended for most users):
```bash
# Install globally - ch-migrate available everywhere
uv tool install clickhouse-alembic

# Verify installation
ch-migrate --version
```

**As a project dependency** (for importing in Python code):
```bash
uv add clickhouse-alembic
uv run ch-migrate --version

# Or with pip
pip install clickhouse-alembic
```

**Pin to a specific version**:
```bash
uv tool install clickhouse-alembic==0.4.1
```

**Unreleased changes from `main`**:
```bash
uv tool install git+https://github.com/DRYCodeWorks/clickhouse-migrate.git
```

### Initialize a Project

```bash
# Create a new project
mkdir my-clickhouse-project
cd my-clickhouse-project

# Initialize with ch-migrate
ch-migrate init

# Or initialize in current directory with custom name
ch-migrate init --name my_project
```

This creates:
```
my-clickhouse-project/
├── config.yaml              # ClickHouse hosts and settings
├── .env.local.example       # Template for secrets
├── .gitignore
├── alembic.ini
└── migrations/
    ├── env.py
    ├── script.py.mako
    ├── versions/            # Migration files go here
    └── sql/
        ├── bootstrap/
        └── history/
            ├── tables/
            ├── views/
            └── dictionaries/
```

### Configure

1. Edit `config.yaml` with your ClickHouse hosts:

```yaml
project:
  name: my_project

defaults:
  port: 8443
  secure: true
  admin_user: default
  # Optional users (uncomment to enable):
  # dict_reader_name: dict_reader
  # mcp_user_name: mcp_reader

environments:
  dev:
    host: my-dev-instance.clickhouse.cloud
    database: my_project_dev
    migration_user: migration_dev

  production:
    host: my-prod-instance.clickhouse.cloud
    database: my_project
    migration_user: migration_prod
```

2. Set up credentials (choose one):

**Option A: Environment file**
```bash
cp .env.local.example .env.local
# Edit with your passwords:
CH_DEV_MIGRATION_PASSWORD=your-dev-password
CH_DEV_ADMIN_PASSWORD=your-dev-admin-password
```

**Option B: AWS SSM** (for production)
```yaml
# In config.yaml, add ssm paths:
environments:
  production:
    host: my-prod-instance.clickhouse.cloud
    database: my_project
    migration_user: migration_prod
    ssm:
      admin_password: /my_project/prod/admin_password
      migration_password: /my_project/prod/migration_password
```

### Bootstrap Database

```bash
# Creates database, roles, and users
ch-migrate bootstrap dev

# Preview without executing
ch-migrate bootstrap dev --dry-run
```

### Create and Run Migrations

```bash
# Create upgrade and downgrade SQL files
ch-migrate new dev create_users_table --table users
# Fill in the generated .up.sql and .down.sql files before running up.

# Run pending migrations
ch-migrate up dev

# Check status
ch-migrate status dev

# Rollback last migration
ch-migrate down dev

# Show history
ch-migrate history dev
```

## Migration Patterns

### SQL-first authoring

`ch-migrate new dev add_status --table logs` creates an Alembic revision and two
SQL files in `migrations/sql/history/tables/logs/`. Edit the `.up.sql` and
`.down.sql` files; the generated Python revision needs no changes. Filenames
include the timestamp, revision, and a message slug of at most 40 characters.

Use `--view NAME` or `--dict NAME` for those object histories. Without an object,
SQL files go in `history/other/`. Only one object option may be supplied.

Use `--irreversible "reason"` when the change cannot be undone. It creates only
an upgrade SQL file and installs both the static marker and downgrade backstop.
An empty reason is rejected. `--python` retains the Python template and optional
single SQL history file. `--exchange --table NAME` retains the exchange scaffold.
`--python`, `--exchange`, and `--irreversible` are mutually exclusive.

### Multi-statement SQL files

Use `run_sql` to execute a file under `migrations/sql/`, one statement per request:

```python
from clickhouse_alembic import run_sql

def upgrade():
    run_sql("history/tables/logs/001_add_status.up.sql")
```

Semicolons inside strings, quoted identifiers, comments, and heredocs do not
split statements. Empty or comment-only files fail rather than recording an
unfilled migration as applied. Execution stops at the first failed statement.

`run_sql` substitutes `{db}`, `{cluster}`, and `{on_cluster}` from the environment.
Keyword arguments add or override substitutions. Other braces remain literal,
including JSON and ClickHouse query parameters such as `{id:UInt64}`. Doubled
braces are not format escapes. `read_sql` retains its existing `str.format`
behavior.

To render SQL without executing it, set `CH_ENVIRONMENT` and run
`alembic upgrade head --sql`. Existing projects must run `ch-migrate upgrade-env`
to install the offline version-table and literal-rendering fixes. Offline
rendering requires Alembic 1.14 or later, included in the package requirements.

### Basic Table Creation

1. Create SQL file in `migrations/sql/history/tables/users/001_<revision>.sql`:

```sql
CREATE TABLE {db}.users (
    id UInt64,
    email String,
    name String,
    created_at DateTime DEFAULT now()
)
ENGINE = MergeTree
ORDER BY id
```

2. Reference in migration:

```python
from alembic import op
from clickhouse_alembic import get_db, read_sql

def upgrade():
    db = get_db()
    op.execute(read_sql("history/tables/users/001_abc123.sql", db=db))

def downgrade():
    db = get_db()
    op.execute(f"DROP TABLE IF EXISTS {db}.users")
```

### Zero-Downtime Schema Changes

Use `EXCHANGE TABLES` for data-preserving migrations:

```python
def upgrade():
    db = get_db()

    # 1. Create new table with updated schema
    op.execute(read_sql("history/tables/users/002_def456.sql", db=db)
               .replace(f"{db}.users", f"{db}.users_new"))

    # 2. Migrate data with transformations
    op.execute(f"""
        INSERT INTO {db}.users_new
        SELECT id, email, name, created_at, '' as phone
        FROM {db}.users
    """)

    # 3. Atomic swap
    op.execute(f"EXCHANGE TABLES {db}.users AND {db}.users_new")

    # 4. Drop old table
    op.execute(f"DROP TABLE {db}.users_new")
```

### Dictionary with Auto-Grant

```python
from clickhouse_alembic import create_dictionary

def upgrade():
    # Automatically grants SELECT to dict_reader on source table
    create_dictionary("history/dictionaries/dict_users/001_abc123.sql")
```

### Irreversible migrations

When a downgrade cannot restore data, declare a module-level reason and raise the
backstop exception:

```python
from clickhouse_alembic import IrreversibleMigration

irreversible = "Drops legacy data"

def downgrade():
    raise IrreversibleMigration(revision, irreversible)
```

`ch-migrate down` reads markers without importing migration files. It refuses
the entire requested range before running Alembic if any revision is
irreversible. It understands `-N` on linear history, full or unique-prefix
revision IDs, and `base`. For an unknown range, including a relative target
across a merge point, it prints a note and relies on each migration's backstop.
Direct Alembic calls also rely on that exception.

There is no override flag. To revert past an irreversible revision, implement
its downgrade and remove its marker in a reviewed change. A marker of `True`
is accepted and reported as "(no reason given)". Exchange scaffolds include
both a reason and the exception.

## CLI Reference

```bash
# Initialize new project
ch-migrate init [PATH] [--name NAME]

# Bootstrap database (create DB, roles, users)
ch-migrate bootstrap <environment> [--dry-run]

# Apply pending migrations
ch-migrate up <environment>

# Rollback last migration
ch-migrate down <environment> [--revision REV]

# Show current status
ch-migrate status <environment>

# Show migration history
ch-migrate history <environment>

# Create a SQL-first migration (or opt into Python/exchange authoring)
ch-migrate new <environment> <name> [--table NAME | --view NAME | --dict NAME]
# Additional choices: --irreversible REASON | --python | --exchange
```

## Configuration Reference

### config.yaml

```yaml
project:
  name: my_project          # Project identifier

defaults:                   # Inherited by all environments
  port: 8443
  secure: true
  admin_user: default
  # dict_reader_name: dict_reader    # Uncomment to enable
  # mcp_user_name: mcp_reader        # Uncomment to enable

environments:
  dev:
    host: dev.clickhouse.cloud
    database: my_project_dev
    migration_user: migration_dev
    # SSM paths (optional, if set fetches from SSM directly):
    # ssm:
    #   admin_password: /my_project/dev/admin_password
    #   migration_password: /my_project/dev/migration_password
```

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `CH_<ENV>_MIGRATION_PASSWORD` | Yes | Migration user password |
| `CH_<ENV>_ADMIN_PASSWORD` | For bootstrap | Admin password for creating users |
| `CH_<ENV>_DICT_READER_PASSWORD` | If dict_reader enabled | dict_reader user password |
| `CH_<ENV>_MCP_PASSWORD` | If mcp_user enabled | MCP user password |

Legacy `CH_<ENV>_PASSWORD` is supported for backward compatibility.

## Roles and Users

Bootstrap creates the following roles:

| Role | Purpose |
|------|---------|
| `{project}_migration_role` | Full migration access, including explicit `system.grants` introspection (required) |
| `{project}_readonly_role` | Read-only for MCP tools (optional) |
| `{project}_dict_role` | Dictionary source access (optional) |

## ClickHouse Cloud Notes

- Uses standard engines (`MergeTree`, `ReplacingMergeTree`) - auto-upgraded to `Shared*` variants on Cloud
- Native port 9440 maps to HTTP port 8443
- No transactional DDL - migrations can't be atomically rolled back
- Each `op.execute()` runs one statement (no multi-statement batches)

## Development

Run the unit suite without starting Docker:

```bash
uv run --locked pytest -q
```

Run the integration suite with Docker available:

```bash
uv run --locked pytest -q -m integration
```

The integration fixtures start a `chm-it-*` container on a random loopback port.
Each test uses its own database. Pytest finalizers remove the database and
container on success, failure, or a handled interrupt. A forced process kill
cannot run finalizers.

`CH_MIGRATE_IT_IMAGE` selects the ClickHouse image tag (default: `26.3`).
If Docker is unavailable, integration tests skip with a reason. To use a dedicated
test server instead, set `CH_MIGRATE_IT_URL` in the environment to an HTTP(S) URL
with credentials. The suite creates and drops databases on that server; never
point it at a shared or production server, and never commit its credentials.

## License

MIT License - see [LICENSE](LICENSE) file for details.

## Author

Dan Young
