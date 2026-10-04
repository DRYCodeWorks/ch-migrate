-- Bootstrap SQL Reference
-- This file documents the SQL structure created by `ch-migrate bootstrap`
-- The actual SQL is generated dynamically by bootstrap.py
--
-- IMPORTANT: This file is NOT executed directly. It serves as documentation.
-- To run bootstrap, use: ch-migrate bootstrap <environment>

-- =============================================================================
-- DATABASE
-- =============================================================================

CREATE DATABASE IF NOT EXISTS {db};

-- =============================================================================
-- ROLES (always created)
-- =============================================================================

-- Migration role: full access for schema changes and data operations
CREATE ROLE IF NOT EXISTS {project}_migration_role;
GRANT ALL ON {db}.* TO {project}_migration_role;
GRANT CREATE TEMPORARY TABLE ON *.* TO {project}_migration_role;
GRANT CURRENT GRANTS(SELECT ON system.*) TO {project}_migration_role WITH GRANT OPTION;
-- ClickHouse Cloud requires explicit grants for individual system tables.
GRANT SELECT ON system.grants TO {project}_migration_role;
GRANT SELECT ON system.databases TO {project}_migration_role;
GRANT SELECT ON system.tables TO {project}_migration_role;
GRANT SELECT ON system.mutations TO {project}_migration_role;
GRANT SELECT ON system.columns TO {project}_migration_role;
GRANT CURRENT GRANTS(SELECT ON system.replicas) TO {project}_migration_role;
GRANT CURRENT GRANTS(SELECT ON system.clusters) TO {project}_migration_role;
GRANT CURRENT GRANTS(SELECT ON system.distributed_ddl_queue) TO {project}_migration_role;
GRANT CURRENT GRANTS(SELECT ON system.parts) TO {project}_migration_role;
GRANT CURRENT GRANTS(SELECT ON system.parts_columns) TO {project}_migration_role;
GRANT CURRENT GRANTS(SELECT ON system.disks) TO {project}_migration_role;
GRANT CURRENT GRANTS(SELECT ON system.merge_tree_settings) TO {project}_migration_role;
GRANT CURRENT GRANTS(SELECT ON system.settings) TO {project}_migration_role;
GRANT CURRENT GRANTS(SELECT ON system.settings_profiles) TO {project}_migration_role;
GRANT CURRENT GRANTS(SELECT ON system.settings_profile_elements) TO {project}_migration_role;
GRANT CURRENT GRANTS(SELECT ON system.users) TO {project}_migration_role;
GRANT CURRENT GRANTS(SELECT ON system.role_grants) TO {project}_migration_role;
GRANT CURRENT GRANTS(SELECT ON system.query_log) TO {project}_migration_role;
GRANT CURRENT GRANTS(SELECT ON system.asynchronous_inserts) TO {project}_migration_role;
GRANT CURRENT GRANTS(SELECT ON system.asynchronous_insert_log) TO {project}_migration_role;
GRANT CURRENT GRANTS(SELECT ON system.processes) TO {project}_migration_role;
GRANT CURRENT GRANTS(KILL QUERY ON *.*) TO {project}_migration_role;
GRANT CURRENT GRANTS(SYSTEM FLUSH LOGS ON *.*) TO {project}_migration_role;
GRANT CURRENT GRANTS(SYSTEM FLUSH ASYNC INSERT QUEUE ON *.*) TO {project}_migration_role;
GRANT CURRENT GRANTS(SYSTEM MERGES ON {db}.*) TO {project}_migration_role;
GRANT CURRENT GRANTS(SYSTEM SYNC REPLICA ON {db}.*) TO {project}_migration_role;
GRANT CURRENT GRANTS(SYSTEM SYNC DATABASE REPLICA ON {db}.*) TO {project}_migration_role;
GRANT CURRENT GRANTS(CLUSTER ON *.*) TO {project}_migration_role;
GRANT CURRENT GRANTS(READ ON REMOTE) TO {project}_migration_role;

-- =============================================================================
-- USERS (always created)
-- =============================================================================

-- Migration user
CREATE USER IF NOT EXISTS {migration_user}
IDENTIFIED BY '{migration_password}';
GRANT {project}_migration_role TO {migration_user};

-- =============================================================================
-- OPTIONAL: MCP User (only if mcp_user_name configured in config.yaml)
-- =============================================================================

-- CREATE ROLE IF NOT EXISTS {project}_readonly_role;
-- GRANT SELECT ON {db}.* TO {project}_readonly_role;
-- GRANT SHOW TABLES ON {db}.* TO {project}_readonly_role;
--
-- CREATE USER IF NOT EXISTS {mcp_user_name}
-- IDENTIFIED BY '{mcp_password}';
-- GRANT {project}_readonly_role TO {mcp_user_name};

-- =============================================================================
-- OPTIONAL: Dict Reader (only if dict_reader_name configured in config.yaml)
-- =============================================================================

-- CREATE ROLE IF NOT EXISTS {project}_dict_role;
-- SELECT grants added per-table when dictionaries are created
--
-- CREATE USER IF NOT EXISTS {dict_reader_name}
-- IDENTIFIED BY '{dict_reader_password}';
-- GRANT {project}_dict_role TO {dict_reader_name};
