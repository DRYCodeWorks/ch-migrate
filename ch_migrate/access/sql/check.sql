-- What `ch_migrate/access/check.py` sends, as the migration user. It only reads.
-- `check.py` fills in privilege names from the server's own list, and quoted names.

-- 1 when the current user holds every privilege named on the target, else 0. It takes no
-- WITH GRANT OPTION.
-- check_grant
CHECK GRANT {grants};
