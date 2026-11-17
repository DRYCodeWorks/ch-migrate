-- What `ch_migrate/access/live.py` reads from the server. Every statement only reads.
-- {names:Array(String)} and {sources:Array(String)} are bound by the server.

-- The privilege tree of this server: names, aliases, levels and groups (design note, 2.7).
-- privileges
SELECT privilege, aliases, level, parent_group
FROM system.privileges;

-- Where a CREATE USER or CREATE ROLE is stored: the first storage, by precedence, that a
-- create can go to. `local_directory` is each node's disk, `replicated` is Keeper, and
-- `memory` is each node's memory. users.xml, LDAP and ClickHouse Cloud's `jwt` only read.
-- storage
SELECT type
FROM system.user_directories
WHERE type IN ('local_directory', 'replicated', 'memory')
ORDER BY precedence
LIMIT 1;

-- 1 when this login holds CLUSTER on *.*, which also holds where the server grants it to
-- every login (`on_cluster_queries_require_cluster_grant` off, as on ClickHouse Cloud).
-- cluster_check
CHECK GRANT CLUSTER ON *.*;

-- This login's own grants, and those of the roles it has enabled, nested ones included.
-- own_grants
SELECT access_type, access_object, database, table, column, is_partial_revoke, grant_option
FROM system.grants
WHERE user_name = currentUser() OR role_name IN enabledRoles();

-- The clusters that hold this node, each with its hosts as `host:port`. Of those that list
-- another host too, one, or several that list the same hosts, is where every replica is read.
-- clusters
SELECT cluster, arraySort(groupUniqArray(concat(host_name, ':', toString(port)))) AS hosts
FROM system.clusters
GROUP BY cluster
HAVING max(is_local) = 1
ORDER BY cluster;

-- The node this login reads, named where users and roles are on each node's own disk
-- (Decision 14).
-- node
SELECT hostName() AS name;

-- roles
SELECT name
FROM system.roles
WHERE name IN {names:Array(String)};

-- With default_roles_all = 1, a role granted later becomes a default role at once, unless
-- default_roles_except names it.
-- users
SELECT name, default_roles_all, default_roles_except
FROM system.users
WHERE name IN {names:Array(String)};

-- A row with is_partial_revoke = 1 takes a right away inside a broader grant; with
-- grant_option = 1 it takes away only the grant option.
-- grants
SELECT user_name, role_name, access_type, access_object, database, table, column,
       is_partial_revoke, grant_option
FROM system.grants
WHERE user_name IN {names:Array(String)} OR role_name IN {names:Array(String)};

-- The roles each user or role holds.
-- role_grants
SELECT user_name, role_name, granted_role_name, granted_role_is_default, with_admin_option
FROM system.role_grants
WHERE user_name IN {names:Array(String)} OR role_name IN {names:Array(String)};

-- The data sources this server's parser takes as a grant target, such as REMOTE.
-- system.privileges doesn't list them all, and the statement is parsed, never run.
-- accepted_sources
SELECT source
FROM (SELECT arrayJoin({sources:Array(String)}) AS source)
WHERE formatQueryOrNull(concat('GRANT READ ON ', source, ' TO probe')) IS NOT NULL;

-- Every replica of {cluster:String}, by the name it gives itself. Each read of every replica
-- runs with skip_unavailable_shards = 0, so a replica that can't be reached is an error and
-- never reads as one that holds nothing. Every server lists users.xml here.
-- replicas
SELECT DISTINCT materialize(hostName()) AS replica
FROM clusterAllReplicas({cluster:String}, system.user_directories);

-- The reads above, of every replica at once, each row tagged with the replica it came from.
-- replica_roles
SELECT materialize(hostName()) AS replica, name
FROM clusterAllReplicas({cluster:String}, system.roles)
WHERE name IN {names:Array(String)};

-- replica_users
SELECT materialize(hostName()) AS replica, name, default_roles_all, default_roles_except
FROM clusterAllReplicas({cluster:String}, system.users)
WHERE name IN {names:Array(String)};

-- replica_grants
SELECT materialize(hostName()) AS replica, user_name, role_name, access_type, access_object,
       database, table, column, is_partial_revoke, grant_option
FROM clusterAllReplicas({cluster:String}, system.grants)
WHERE user_name IN {names:Array(String)} OR role_name IN {names:Array(String)};

-- replica_role_grants
SELECT materialize(hostName()) AS replica, user_name, role_name, granted_role_name,
       granted_role_is_default, with_admin_option
FROM clusterAllReplicas({cluster:String}, system.role_grants)
WHERE user_name IN {names:Array(String)} OR role_name IN {names:Array(String)};
