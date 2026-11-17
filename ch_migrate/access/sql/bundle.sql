-- What `ch_migrate/access/bundle.py` runs to record the bundled privilege list.
-- Only the scratch role it is given is ever written to.

-- privileges
SELECT privilege, aliases, level, parent_group
FROM system.privileges
ORDER BY privilege;

-- create_scratch_role
CREATE ROLE IF NOT EXISTS {role};

-- clear_scratch_role
REVOKE ALL ON *.* FROM {role};

-- grant_on_everything
GRANT {privilege} ON *.* TO {role};

-- scratch_grants
SELECT access_type, access_object
FROM system.grants
WHERE role_name = {role:String};

-- drop_scratch_role
DROP ROLE IF EXISTS {role};
