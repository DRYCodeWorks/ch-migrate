-- What `ch_migrate/access/applier.py` sends, for `access apply` and `bootstrap`, named by
-- the comment above each statement.
-- `writes.py` fills in names already quoted and privileges the server's own list knows.
-- {cluster} is empty, or ON CLUSTER and the cluster's quoted name where users and roles
-- live on each node's disk. Quoted, a name such as `c-1` parses.
-- A password is never part of the text: the server binds {password:String}.

-- The login `apply` is connected as, which access.yaml can't declare.
-- current_user
SELECT currentUser() AS name;

-- The roles the login holds, through other roles too: `apply` won't change them.
-- current_roles
SELECT arrayJoin(enabledRoles()) AS name;

-- Only `bootstrap` sends this, before anything else (design note, 2.5).
-- create_database
CREATE DATABASE IF NOT EXISTS {name}{cluster};

-- create_role
CREATE ROLE IF NOT EXISTS {name}{cluster};

-- create_user
CREATE USER IF NOT EXISTS {name}{cluster} IDENTIFIED BY {password:String};

-- reset_password
ALTER USER {name}{cluster} IDENTIFIED BY {password:String};

-- grant_role
GRANT{cluster} {role} TO {grantee};

-- grant
GRANT{cluster} {grants} TO {grantee};

-- grant_with_grant_option
GRANT{cluster} {grants} TO {grantee} WITH GRANT OPTION;

-- Narrowing a scope: the whole declared set in one statement, which takes away the rest.
-- replace
GRANT{cluster} {grants} TO {grantee} WITH REPLACE OPTION;

-- A best-effort grant lands what the admin holds of it and raises no error for the rest.
-- ClickHouse refuses it ON CLUSTER, so there each privilege is a plain `grant` instead.
-- best_effort
GRANT CURRENT GRANTS({grants}) TO {grantee};

-- best_effort_with_grant_option
GRANT CURRENT GRANTS({grants}) TO {grantee} WITH GRANT OPTION;

-- SET DEFAULT ROLE takes no ON CLUSTER; ALTER USER does. {roles} is ALL, NONE or a list.
-- set_default_roles
ALTER USER {name}{cluster} DEFAULT ROLE {roles};

-- revoke
REVOKE{cluster} {grants} FROM {grantee};

-- revoke_grant_option
REVOKE{cluster} GRANT OPTION FOR {grants} FROM {grantee};

-- revoke_role
REVOKE{cluster} {role} FROM {grantee};

-- revoke_admin_option
REVOKE{cluster} ADMIN OPTION FOR {role} FROM {grantee};

-- drop_user
DROP USER IF EXISTS {name}{cluster};

-- drop_role
DROP ROLE IF EXISTS {name}{cluster};
