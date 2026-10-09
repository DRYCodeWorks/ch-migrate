# ch-migrate Quick Reference

## New Project Setup

```bash
# 1. Install
uv add ch-migrate-cli        # or: pip install ch-migrate-cli

# 2. Initialize
ch-migrate init                   # creates project structure in current dir
ch-migrate init ./migrations      # or specify a path

# 3. Configure
# Edit config.yaml with your ClickHouse host
# Copy .env.local.example to .env.local, add passwords

# 4. Bootstrap (creates DB, roles, users)
ch-migrate bootstrap dev --dry-run   # preview first
ch-migrate bootstrap dev             # execute

# 5. Create first migration
ch-migrate new dev create_users_table
# Edit the generated migration file

# 6. Run migration
ch-migrate up dev
```

## Existing Project Integration

```bash
# 1. Install in your project
uv add ch-migrate-cli

# 2. Initialize in a subdirectory (or root)
ch-migrate init ./migrations --name my_project

# 3. Configure for your existing ClickHouse
# Edit migrations/config.yaml
# Create migrations/.env.local

# 4. Bootstrap (safe to run on existing DB)
ch-migrate bootstrap dev

# 5. Create baseline migration (captures existing schema)
ch-migrate new dev baseline
# Edit to include existing tables as no-ops or document current state

# 6. Run to mark baseline as applied
ch-migrate up dev
```

## Daily Commands

| Command | Description |
|---------|-------------|
| `ch-migrate status dev` | Show current migration state |
| `ch-migrate new dev <name>` | Create new migration |
| `ch-migrate up dev` | Apply pending migrations |
| `ch-migrate down dev` | Rollback last migration |
| `ch-migrate down dev -r <rev>` | Rollback to specific revision |
| `ch-migrate history dev` | Show migration history |

## Troubleshooting

```bash
# Check if .env.local is loaded
ch-migrate bootstrap dev --dry-run   # should show masked passwords

# Verify connection
ch-migrate status dev

# See what SQL would run
ch-migrate bootstrap dev --dry-run

# Check ClickHouse directly
curl "http://localhost:8123/?user=default&password=xxx" -d "SHOW DATABASES"
```

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `CH_<ENV>_MIGRATION_PASSWORD` | Yes | Migration user password |
| `CH_<ENV>_ADMIN_PASSWORD` | Bootstrap | Admin password for setup |
| `CH_<ENV>_MCP_PASSWORD` | If configured | Read-only user password |

Example for dev environment:
```bash
CH_DEV_MIGRATION_PASSWORD=secret123
CH_DEV_ADMIN_PASSWORD=admin123
```

---

See [Appendices](#appendices) for detailed guides.

# Appendices

## Appendix A: Greenfield Setup with Docker

### 1. Start ClickHouse

```bash
docker run -d \
  --name clickhouse \
  -p 8123:8123 \
  -e CLICKHOUSE_PASSWORD=admin123 \
  clickhouse/clickhouse-server:latest
```

### 2. Create Project

```bash
mkdir my-project && cd my-project
uv init && uv add ch-migrate-cli
ch-migrate init --name my_project
```

### 3. Configure for Local Docker

**config.yaml:**
```yaml
project:
  name: my_project

defaults:
  port: 8123
  secure: false
  admin_user: default

environments:
  dev:
    host: localhost
    database: my_project_dev
    migration_user: migration_dev
```

**.env.local:**
```bash
CH_DEV_ADMIN_PASSWORD=admin123
CH_DEV_MIGRATION_PASSWORD=migpass123
```

### 4. Bootstrap and Migrate

```bash
ch-migrate bootstrap dev
ch-migrate new dev create_users_table
# Edit the migration...
ch-migrate up dev
```

---

## Appendix B: Existing Project Integration

### Scenario: Monorepo with Existing ClickHouse

```
my-monorepo/
├── services/
├── packages/
└── migrations/          # <-- add here
    ├── config.yaml
    ├── .env.local
    └── migrations/
```

### 1. Initialize

```bash
cd my-monorepo
ch-migrate init ./migrations --name my_project
```

### 2. Connect to Existing ClickHouse

**migrations/config.yaml:**
```yaml
project:
  name: my_project

defaults:
  port: 8443
  secure: true
  admin_user: default

environments:
  dev:
    host: my-dev-instance.clickhouse.cloud
    database: existing_database
    migration_user: migration_dev

  staging:
    host: my-staging-instance.clickhouse.cloud
    database: existing_database
    migration_user: migration_staging

  production:
    host: my-prod-instance.clickhouse.cloud
    database: existing_database
    migration_user: migration_prod
```

### 3. Create Baseline Migration

If you have existing tables, create a baseline migration that documents the current state:

```bash
ch-migrate new dev baseline_existing_schema
```

Edit the migration to either:
- **Option A:** Include CREATE TABLE statements (will no-op if tables exist)
- **Option B:** Leave empty and just document existing schema

```python
def upgrade() -> None:
    # Existing tables as of 2024-01-08:
    # - users (id, email, name, created_at)
    # - events (id, user_id, event_type, timestamp)
    # These were created manually before migrations were adopted.
    pass

def downgrade() -> None:
    # Cannot downgrade - these tables existed before migrations
    raise NotImplementedError("Cannot downgrade baseline migration")
```

---

## Appendix C: Environment Configuration

### Local Development (.env.local)

```bash
# .env.local - gitignored, local secrets only
CH_DEV_ADMIN_PASSWORD=local-admin-pass
CH_DEV_MIGRATION_PASSWORD=local-migration-pass
CH_DEV_MCP_PASSWORD=local-mcp-pass
```

### Staging/Production (AWS SSM)

**config.yaml:**
```yaml
environments:
  production:
    host: prod.clickhouse.cloud
    database: my_project
    migration_user: migration_prod
    ssm:
      admin_password: /my_project/prod/admin_password
      migration_password: /my_project/prod/migration_password
      mcp_password: /my_project/prod/mcp_password
```

SSM support (`boto3`) is included in the base install; AWS credentials come from your usual AWS profile or environment.

**Set up SSM parameters:**
```bash
aws ssm put-parameter \
  --name "/my_project/prod/migration_password" \
  --value "secret" \
  --type SecureString
```

### Priority Order

1. SSM parameters (if `ssm:` paths configured in config.yaml)
2. Environment variables (`CH_<ENV>_*`) - used when no SSM paths
3. Error if required and not found

---

## Appendix D: Rollback Patterns

### ClickHouse DDL is Non-Transactional

Unlike PostgreSQL, ClickHouse cannot roll back DDL. If a migration fails halfway:
- Some statements may have executed
- You'll need to manually fix or continue

### Safe Rollback Pattern

Always write thorough `downgrade()` functions:

```python
def upgrade() -> None:
    db = get_db()
    op.execute(f"""
        CREATE TABLE {db}.users (
            id UInt64,
            email String,
            created_at DateTime DEFAULT now()
        ) ENGINE = MergeTree() ORDER BY id
    """)

def downgrade() -> None:
    db = get_db()
    op.execute(f"DROP TABLE IF EXISTS {db}.users")
```

### Zero-Downtime Schema Changes

Use `EXCHANGE TABLES` for safe migrations:

```python
def upgrade() -> None:
    db = get_db()

    # 1. Create new table with updated schema
    op.execute(f"""
        CREATE TABLE {db}.users_new (
            id UInt64,
            email String,
            phone String,  -- new column
            created_at DateTime DEFAULT now()
        ) ENGINE = MergeTree() ORDER BY id
    """)

    # 2. Copy data with transformation
    op.execute(f"""
        INSERT INTO {db}.users_new
        SELECT id, email, '' as phone, created_at
        FROM {db}.users
    """)

    # 3. Atomic swap
    op.execute(f"EXCHANGE TABLES {db}.users AND {db}.users_new")

    # 4. Drop old
    op.execute(f"DROP TABLE {db}.users_new")
```

---

## Appendix E: Read-Only User Setup

### Enable MCP User

**config.yaml:**
```yaml
defaults:
  mcp_user_name: mcp_reader  # uncomment to enable

environments:
  dev:
    # ... other config
```

**.env.local:**
```bash
CH_DEV_MCP_PASSWORD=readonly-pass
```

### What Gets Created

Bootstrap creates:
- `{project}_readonly_role` with SELECT + SHOW TABLES
- `mcp_reader` user with the readonly role

### Use Cases

- **BI Tools:** Connect Metabase/Superset with read-only access
- **MCP Servers:** Claude/AI tools can query without write access
- **Dashboards:** Grafana/monitoring with safe credentials
