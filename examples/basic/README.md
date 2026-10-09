# Basic Example

A minimal example showing ch-migrate-cli in action.

## What is ch-migrate?

`ch-migrate` is a CLI tool for managing ClickHouse schema migrations. It:
- Initializes project structure with config files
- Bootstraps databases with proper users and roles
- Runs Alembic migrations against ClickHouse

## Setup

1. Install the package:
   ```bash
   uv add ch-migrate-cli
   # or: pip install ch-migrate-cli
   ```

2. Initialize (already done in this example):
   ```bash
   ch-migrate init
   ```

3. Configure `config.yaml` with your ClickHouse host

4. Set up credentials (choose one):

   **Option A: Environment file**
   ```bash
   cp .env.local.example .env.local
   # Edit .env.local with real passwords
   ```

   **Option B: AWS SSM** (for production)
   ```yaml
   # In config.yaml, add ssm paths:
   environments:
     production:
       ssm:
         admin_password: /myproject/prod/admin_password
         migration_password: /myproject/prod/migration_password
   ```

5. Bootstrap:
   ```bash
   ch-migrate bootstrap dev
   ```

6. Run migrations:
   ```bash
   ch-migrate up dev
   ```

## Files

- `config.yaml` - ClickHouse connection settings (edit hosts)
- `.env.local.example` - Template for secrets
- `migrations/sql/history/tables/users/001_example.sql` - Example table schema
