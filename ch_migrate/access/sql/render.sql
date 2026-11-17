-- The statements `access validate --sql` and `bootstrap --dry-run` print, named by the
-- comment above each.
-- `render.py` fills in names that already passed `validate_identifier`.

-- Only `bootstrap --dry-run` prints this, before anything else.
-- create_database
CREATE DATABASE IF NOT EXISTS {name};

-- create_role
CREATE ROLE IF NOT EXISTS {name};

-- {password} is an unquoted placeholder, so the statement fails to parse until a DBA
-- replaces it with the quoted secret. There is no CREATE USER without IDENTIFIED: on 26.3
-- that is a no_password login anyone can use.
-- create_user
CREATE USER IF NOT EXISTS {name} IDENTIFIED BY {password};

-- grant_roles
GRANT {roles} TO {grantee};

-- grant
GRANT {privileges} ON {target} TO {grantee};

-- grant_with_grant_option
GRANT {privileges} ON {target} TO {grantee} WITH GRANT OPTION;

-- grant_best_effort
GRANT CURRENT GRANTS({privileges} ON {target}) TO {grantee};

-- grant_best_effort_with_grant_option
GRANT CURRENT GRANTS({privileges} ON {target}) TO {grantee} WITH GRANT OPTION;

-- set_default_roles
SET DEFAULT ROLE {roles} TO {user};

-- set_no_default_roles
SET DEFAULT ROLE NONE TO {user};

-- drop_user
DROP USER IF EXISTS {name};

-- drop_role
DROP ROLE IF EXISTS {name};
