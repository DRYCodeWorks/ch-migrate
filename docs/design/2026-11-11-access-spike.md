# Spike: how ClickHouse stores and checks grants (U1–U10)

**Date:** 2026-11-11 · **For:** Part 3 of `2026-11-11-declarative-access.md` (the design note)
**Setup:** Docker only. The `26.3`, `25.8` and `24.8` image tags floated to 26.3.62.3, 25.8.33.6
and 24.8.14.39. Cluster answers use the two-node fixture in `tests/integration/conftest.py`,
once with users and roles on each node's disk (`clickhouse_cluster`) and once with them in
Keeper (`clickhouse_cluster_keeper`).

Every answer below names the test in `tests/integration/test_access_spike.py` that keeps
asserting it. `tests/integration/grant_rights.py` holds the comparison those tests use. It is
test code; section "The comparison" describes it for T4 to rebuild in the product.

## Summary

- **Narrowing a grant's scope can't follow "grant before revoke" (U1).** Take a role that holds
  `SELECT ON db.*` while the YAML asks for `SELECT ON db.events`. Granting `db.events` first
  writes nothing, because `db.*` absorbs it, and revoking `db.*` then removes both. Two orders
  converge:
  - revoke, then grant, with a moment of no access in between;
  - one `GRANT <the whole desired set> TO x WITH REPLACE OPTION`, which is atomic and keeps
    role memberships. It works only if the admin holds every right the grantee has now,
    with grant option. If it doesn't, the statement fails and nothing changes.

  This bore on Decision 3 ("It grants before it revokes") and on 2.5's apply order. It
  was decided that `apply` narrows a scope with one `WITH REPLACE OPTION` of the whole
  declared set.
  If the admin lacks a right the grantee holds, nothing changes and `apply` says why, and it
  is never used on the migration role (2026-11-11; Decision 3). Change 5 records it.
- **A read-only comparison works (U1).** Expanding both sides to one right per privilege
  flag, scope and grant option, using the server's own `system.privileges`, reproduces what
  `system.grants` holds. It held for every privilege on `*.*`, `db.*` and `db.t` on 26.3,
  and for both profiles on all three images. There is one exception: on 25.8 and 26.3,
  `<SOURCE> ON *.*` is stored as `READ, WRITE ON <SOURCE>`.
- **No other answer bears on a Part 1 decision.** Five answers confirm Part 2 as written: U2,
  U3, U4, U6 and U10. U1, U5, U7, U8 and U9 add the named changes listed under "Changes to
  the design note".
- **An `ON CLUSTER` error can lose its code (U9).** Without the setting
  `wait_end_of_query=1`, a host that fails after another host has answered reaches the client
  as a broken HTTP response with no error code. `up` already sends the setting; T5's `apply`
  must too (change 6).
- **On a self-hosted cluster, `up` needs four of the best-effort grants (U7):** `CLUSTER`,
  `READ ON REMOTE`, `SYSTEM SYNC REPLICA` and `SELECT ON system.replicas`. A single node needs
  none of them. The design note already defers "cluster grants that become required only when
  a cluster applies" (2.9); this names them.
- **24.8 lacks `CHECK GRANT` and `READ ON <source>` (U5, U8).** 0.8's own `bootstrap` stops
  there at `READ ON REMOTE`, before it creates any user. The live `access` commands need
  25.8 or later, and exit 2 on an older server (2026-11-11; Decision 11).

## The comparison

T4's live reader and planner should do this.

1. **Read the tree.** `SELECT privilege, aliases, level, parent_group FROM system.privileges` on
   the server being compared. Don't ship a list for `plan`: the three images have different
   privileges and levels (U8). `validate`, which has no server, checks names against a
   bundled list taken from 26.3's `system.privileges`, and an unknown name is a warning
   (2026-11-11).
2. **Flags.** A privilege whose `level` is not NULL carries a flag of its own. A privilege
   with a NULL level is a group, with no flag of its own. A privilege means its own flag plus
   the flags of all its descendants, through `parent_group`. Some privileges are both: for
   example, `CREATE ARBITRARY TEMPORARY TABLE` has a level and a child.
3. **Scope filter.** Keep only the flags that the target's scope allows:
   - `*.*`: every level.
   - `db.*`: `DATABASE`, `TABLE`, `DICTIONARY`, `VIEW`, `COLUMN`.
   - `db.table`: `TABLE`, `DICTIONARY`, `VIEW`, `COLUMN`.
   - a source (`READ ON REMOTE`): `SOURCE`.

   So `CREATE ON db.*` means `CREATE DATABASE`, `CREATE TABLE`, `CREATE VIEW` and
   `CREATE DICTIONARY`. `ALTER ON db.table` drops `ALTER DATABASE`. The server stores exactly
   those rows, and it rejects a grant whose filtered set is empty (`INVALID_GRANT` or
   `SYNTAX_ERROR`).
4. **Rights.** A right is `(flag, scope, grant option)`. A scope is the database, the table,
   the column and `access_object` from `system.grants`. NULL means "every", and
   `access_object` holds the source for `READ` and `WRITE`. A column scope is narrower than
   its table, so a live `SELECT(id) ON db.events` doesn't satisfy a desired table grant, and
   granting the table grant absorbs it. v1 declares no column grants.
   - Desired rights come from each grant in `access.yaml` and the profile.
   - Live rights come from each `system.grants` row of the user or role.
   - A row with `is_partial_revoke = 1` is a revoke. With `grant_option = 1` it revokes only
     the grant option.
5. **Compare by coverage, both ways.** Right A covers right B when they have the same flag, A's
   scope contains B's, and A has grant option whenever B does.
   - **Missing:** desired rights no live right covers.
   - **Extra:** live rights no desired right covers.
   - **Partial revokes:** every revoke row. A desired set never has one, so any revoke row
     on a declared user or role is drift.

   Coverage makes folding cancel out:
   - `SELECT ON system.tables` is covered by `SELECT ON system.*`, which holds no row for it;
   - `SELECT` is covered by `SELECT ... WITH GRANT OPTION`;
   - `DELETE`, stored as `ALTER DELETE`, is covered by `ALTER`.
6. **Aliases.** Look a name up among both `privilege` and `aliases`, ignoring case and extra
   spaces. `DELETE` becomes `ALTER DELETE`, and `SYNC REPLICA` becomes `SYSTEM SYNC REPLICA`.
7. **Old source spellings.** On servers that have a `SOURCE`-level `READ` (25.8 and 26.3),
   `REMOTE ON *.*`, `S3 ON *.*` and the other sources are stored as `READ` and `WRITE` on that
   source, and `SOURCES ON *.*` as `READ` and `WRITE` on every source. The tree can't say which
   privileges are sources, so a desired `REMOTE ON *.*` can't be expanded to match.
   `validate` rejects that spelling, and its error names the new one, `READ, WRITE ON REMOTE`
   (2026-11-11; change 1). On those
   servers the reader never meets the old spelling, because the server has already
   rewritten it. On 24.8, `REMOTE` is an ordinary global privilege and expands like
   any other.
8. **Changes.**
   - Grant each missing right.
   - Revoke each extra flag on its own scope. `REVOKE` accepts a list of flags, and the server
     stores what is left of a group as its children. Never revoke a group that the desired
     set only partly drops.
   - An extra that differs from a desired right only by grant option takes
     `REVOKE GRANT OPTION FOR`.
   - Re-grant a declared grant to clear a partial revoke (U2).
   - **Narrowing a scope** (`db.*` to `db.events`) can't grant first: see the summary.
     `apply` replaces the whole declared set with `WITH REPLACE OPTION` (2026-11-11).

**How 0.8's statements map to stored rows.** This covers the legacy profile and T1's fixture.
`test_u1_profiles_match_one_grant_at_a_time_and_together` replays each statement of 0.8's
`build_bootstrap_sql`, one at a time and then all together, and step 5 finds no difference.
- `DELETE ON {db}.*` leaves no row. It is `ALTER DELETE`, which `ALTER` covers.
- `ACCESS MANAGEMENT ON {db}.*` leaves no row of its own. On a database it means the four
  row-policy flags, which 0.8 also grants by name. They are stored once.
- `SELECT ON system.grants`, `.databases`, `.tables` and `.mutations`, and the best-effort
  `.replicas` and `.parts`, leave no rows. `SELECT ON system.* WITH GRANT OPTION` covers
  them.
- `CURRENT GRANTS(CREATE USER, ALTER USER, DROP USER ON *.*)` and the other `*.*` grants
  leave one row per privilege.
- `CURRENT GRANTS(READ ON REMOTE)` leaves `READ` with `access_object = 'REMOTE'`. `SHOW GRANTS`
  prints it as `GRANT REMOTE ON *.*`.

## U1. Read-only comparison

**Answer: yes.** The comparison above reproduces `system.grants` after every grant tried:
- folded privileges;
- a parent stored as its children;
- a narrower grant absorbed by a broader one;
- grant option absorbing the plain grant;
- aliases.

**Narrowing a scope** is the exception to "grant before revoke"; the summary has the details.
Both converging orders are under test, and so is the admin limit on `WITH REPLACE OPTION`.
`apply` uses `WITH REPLACE OPTION` (2026-11-11).
`WITH REPLACE OPTION` and several `<privileges> ON <target>` clauses in one `GRANT` also
worked by hand on 24.8 and 25.8.

On 26.3 it held for all 427 accepted grants of every privilege on `*.*`, `db.*` and `db.t`,
apart from the 18 old source spellings in step 7. Each rejected grant either expands to
nothing, or asks for rights that the admin's own rows don't cover with grant option. The
default admin can't pass on the named-collection privileges. Narrowing `ALTER` to
`ALTER UPDATE` works by revoking the other `ALTER` leaves. Granting `ALTER UPDATE` and then
revoking `ALTER` leaves nothing.

`SHOW GRANTS` text is not a reliable record. `READ ON REMOTE` reads back as
`GRANT REMOTE ON *.*`, and `READ ON *.*` as `GRANT SOURCES ON *.*`.

**Tests:**
- `test_u1_every_privilege_on_every_target`;
- `test_u1_profiles_match_one_grant_at_a_time_and_together`;
- `test_u1_folded_grants_still_match`, eight cases;
- `test_u1_show_grants_text_differs_from_what_is_stored`;
- `test_u1_narrowing_revokes_only_the_difference`;
- `test_u1_grant_option_is_part_of_each_right`;
- `test_u1_narrowing_a_scope_cannot_grant_first`;
- `test_u1_replace_needs_every_current_right_with_grant_option`;
- `test_u1_column_grant_is_narrower_than_a_table_grant`.

A hand-run of the same sweep on the other images found:
- **25.8:** the same 18 exceptions as 26.3.
- **24.8:** no stored-row exceptions. Nine privileges were syntax errors on `*.*` although
  they expand to something: the five named-collection ones, `SHOW NAMED COLLECTIONS SECRETS`,
  `SHOW NAMED COLLECTIONS`, `SET DEFINER` and `TABLE ENGINE`. 24.8 takes their targets in
  another form, and v1 grants none of them.

On those images only the profiles are under test, in
`test_u8_leaf_expansion_matches_both_profiles_on_this_image`.

**Means for T4 and T5:** confirms 2.7's first candidate, leaf expansion. The scratch-role and
`CHECK GRANT` candidates are not needed. Changes 1, 2 and 5 below.

## U2. Partial revokes

**Answer:**
- `REVOKE` on a narrower scope than a grant writes a row with `is_partial_revoke = 1`. For
  example, `SELECT ON db.*` minus `db.events`.
- `REVOKE GRANT OPTION FOR` writes a row with `is_partial_revoke = 1` and `grant_option = 1`.
- Revoking a child on the same scope writes no revoke row. The server stores the group as
  its remaining children instead, for example `ALTER` minus `ALTER DELETE`.
- Granting the broader grant again removes the revoke row.

**Tests:**
- `test_u2_partial_revoke_is_a_row_and_granting_again_clears_it`;
- `test_u2_revoking_a_child_at_the_same_scope_leaves_no_partial_revoke`;
- `test_u2_revoking_only_the_grant_option_is_a_partial_revoke_row`.

**Means for T4 and T5:** confirms Part 2. `plan` reports a revoke row on a declared user or role
as drift, a `+` line that re-grants the declared grant. `apply` fixes it by granting again.

## U3. Objects that don't exist yet

**Answer: ClickHouse accepts and stores grants on databases and tables that don't exist.**
- A user holding `CREATE TABLE, SELECT, INSERT` on a database that doesn't exist yet can use
  it once someone creates the database.
- Dropping the database leaves the grant rows in place.
- `CHECK GRANT` answers 1 for a granted object that doesn't exist (U5).

**Test:** `test_u3_grants_on_missing_objects_are_kept_and_work_later`.

**Means for T5 and T6:** confirms Part 2. `access apply` can run before `up`, and on 25.8 and
later `check` works on a fresh database.

## U4. Passwords

**Answer: nothing the admin reads by default shows a password or its hash,** for the default
method, `sha256_password`, `double_sha1_password` and `bcrypt_password`. The test creates each
user with a literal password, as `bootstrap` does, and checks:
- `SHOW CREATE USER`, which prints only the method, even with
  `format_display_secrets_in_show_and_select = 1`. The server setting that would allow it,
  `display_secrets_in_show_and_select`, is 0 in the image.
- `SHOW ACCESS`;
- every column of `system.users`, where `auth_params` is `{}`;
- `system.query_log`, which drops the `BY '...'` clause from the logged query.

None of them holds the password, its SHA-256 or double SHA-1 in hex, or a bcrypt hash.

**Test:** `test_u4_nothing_the_admin_reads_shows_a_password_or_hash`, four methods.

**Means for T4 and T5:** confirms 2.5. `plan` never compares passwords. Passwords are set at
creation, or with `--reset-passwords`.

## U5. `CHECK GRANT`

**Answer:**

| Image | `CHECK GRANT` | A partly held group | An object that doesn't exist | `WITH GRANT OPTION` |
| --- | --- | --- | --- | --- |
| 24.8 | `SYNTAX_ERROR`; it parses as `CHECK TABLE` | — | — | — |
| 25.8 | 1 or 0 | 0, for `ALTER` with only `ALTER UPDATE` held | 1 when granted | `SYNTAX_ERROR` |
| 26.3 | 1 or 0 | 0 | 1 when granted | `SYNTAX_ERROR` |

On 24.8 the migration user can still read its own grants and its roles' grants with
`SHOW GRANTS` and `SHOW GRANTS FOR <role>`. It can't read `system.grants`.

**Test:** `test_u5_check_grant_on_this_image`. It runs on all three images and asserts the
24.8 error instead of skipping.

**Means for T6:** confirms 2.5 on 25.8 and later: `check` runs one `CHECK GRANT` per required
grant, as written, so a partly held group counts as missing. Core has no grant option, so
`CHECK GRANT`'s blindness to it doesn't matter. On 24.8, `check` exits 2 and names the
version, like every live `access` command (2026-11-11; change 3).

## U6. The smallest admin

**Answer:** on single-node Docker, a core-only bootstrap needs exactly these admin grants:
- `CREATE DATABASE ON {db}.*`;
- `CREATE ROLE ON *.*`;
- `CREATE USER ON *.*`;
- `ROLE ADMIN ON *.*`;
- core's required grants `WITH GRANT OPTION`, except the three `SHOW` grants.

Notes on that set:
- **No `SHOW` grants.** ClickHouse implies `SHOW TABLES`, `SHOW COLUMNS` and `SHOW DICTIONARIES`,
  with grant option, from the admin's other rights on the database.
- **`ROLE ADMIN` is needed,** although it is broad. Creating a role doesn't give its creator the
  admin option, so without `ROLE ADMIN` the admin can't grant the new role to the migration user.
  It also lets the admin grant any role to anyone. T7's docs say so (2026-11-11).
- **`CREATE DATABASE` is needed even when the database exists.** `CREATE DATABASE IF NOT EXISTS`
  checks the grant before it checks whether the database exists.

That admin lands every required grant and no best-effort grant. A `CURRENT GRANTS` that lands
nothing raises no error. T1's limited admin can give every core grant except three
best-effort ones: `CLUSTER`, `READ ON REMOTE` and `CREATE TEMPORARY TABLE ON *.*`.

**Tests:**
- `test_u6_smallest_admin_runs_a_core_bootstrap`;
- `test_u6_each_smallest_admin_grant_is_needed`: 11 cases, each leaving one grant out;
- `test_u6_create_database_if_not_exists_needs_the_grant_when_it_exists`;
- `test_u6_the_t1_limited_admin_cannot_give_three_best_effort_grants`.

The core bootstrap is written out by hand in the test, because there is no product code for
it yet.

**Means for T6:** confirms 2.4's split between required and best-effort. T6's limited-admin
tests can use this smaller set. T1's `LIMITED_ADMIN_GRANTS` stays as it is, because it pins
0.8's behavior. A `CURRENT GRANTS` that lands nothing must be detected by reading back, since
the server gives no signal (Decision 6).

## U7. What ch-migrate needs

**Answer on a single node:** a migration user holding only core's required grants runs:
- `new`;
- `up`, with a migration that creates a table, view and dictionary, inserts, adds a column,
  waits on an `ALTER UPDATE`, deletes and optimizes;
- `status`, `history`, `lint`, `snapshot`, `diff` and `new --table`;
- `down`, which truncates and drops.

`rebase` and `deps` weren't run.

Without two of the required grants a path fails:
- `SELECT ON system.mutations`: `up` fails with `ACCESS_DENIED`.
- `SELECT ON system.dictionaries`: `snapshot` fails with `ACCESS_DENIED`.

`SELECT ON system.databases` and `system.tables` are granted implicitly on self-hosted servers,
so `up` and `down` run without them.

**Answer on the local-storage cluster, with `cluster` set:** beyond core required, `up` needs:
- `CLUSTER`, for `ON CLUSTER` DDL;
- `READ ON REMOTE`, for the `clusterAllReplicas` preflight;
- `SYSTEM SYNC REPLICA`, for the lightweight sync of ch-migrate's replicated tables;
- `SELECT ON system.replicas`, while a mutation on a replicated table is pending.

Without each one, `up` fails with `ACCESS_DENIED` naming it. Without `SELECT ON system.parts`,
the waiter still waits. Without `CREATE TEMPORARY TABLE`, `up` and `down` run. The fixture's
database is Atomic, so `SYSTEM SYNC DATABASE REPLICA`, which only a Replicated database uses,
was not exercised.

**Tests:**
- `test_u7_core_required_alone_runs_these_paths`;
- `test_u7_these_system_grants_are_needed`;
- `test_u7_system_databases_and_tables_are_readable_without_a_grant`;
- `test_u7_cluster_up_needs_these_best_effort_grants`;
- `test_u7_cluster_up_runs_without_create_temporary_table`;
- `test_u7_cluster_waiting_on_a_held_mutation`.

The cluster answer depends on the fixture's `remote_servers`, which names a fixed `default`
login for each replica. The spike didn't try other cluster credentials.

**Means for T6:**
- On a single node, U7 confirms 2.4. Keep `system.databases` and `system.tables` required:
  Cloud may not grant them implicitly, and T8 checks that.
- On a self-hosted cluster, it is change 4 below. The four grants stay best-effort, and
  `check` only warns when one is missing (2026-11-11).

## U8. `system.privileges`

**Answer:** all three images have `system.privileges` with the same four columns: `privilege`,
`aliases`, `level` and `parent_group`. Its contents differ:

| | 24.8.14.39 | 25.8.33.6 | 26.3.62.3 |
| --- | --- | --- | --- |
| Privileges | 189 | 211 | 230 |
| `SOURCE` and `DEFINER` levels, `READ` and `WRITE` | no | yes | yes |
| `REMOTE` and the other sources | under the `SOURCES` group | under `ALL` | under `ALL` |
| `CREATE USER` level | `GLOBAL` | `USER_NAME` | `USER_NAME` |
| `system.grants.access_object` | no | yes | yes |
| `GRANT READ ON REMOTE` | `SYNTAX_ERROR` | stored as `READ` / `REMOTE` | the same |
| 0.8 `bootstrap` | exits 1 at `READ ON REMOTE`, no user created | exits 0 | exits 0 |

The counts are what the floating tags held on 2026-11-11. The tests don't assert them.

**Tests:**
- `test_u8_system_privileges_shape_on_this_image`;
- `test_u8_leaf_expansion_matches_both_profiles_on_this_image`: both profiles compare clean on
  each image, without `READ ON REMOTE` on 24.8;
- `test_u8_legacy_bootstrap_on_this_image`.

**Means for T3, T4 and T6:**
- The planner reads the tree from the server (step 1 above), and the reader tolerates a missing
  `access_object` column.
- The live `access` commands need 25.8 or later, and exit 2 on 24.8 (2026-11-11).
  Legacy mode's `READ ON REMOTE` is best-effort under 2.4's rules. Whether `bootstrap` warns
  about it on 24.8 or refuses that server is open question 7.
- `access validate` runs with no server, so it can't read the tree. It checks names against a
  bundled list taken from 26.3's `system.privileges`, and an unknown name is a warning
  (2026-11-11).

## U9. Where users and roles are stored

**Answer:**
- **ch-migrate can tell from the server alone.** `system.user_directories` lists the storages
  by `precedence`. `users_xml` is read-only, so `CREATE` writes to the first other one:
  - `local_directory` with the image's default config;
  - `replicated` when `user_directories` names Keeper.

  `system.roles.storage` and `system.users.storage` give the same answer for one entity.
- **Local storage:** a `CREATE ROLE` or a `GRANT` without `ON CLUSTER` reaches only the node
  that ran it. With `ON CLUSTER`, both reach both nodes.
- **Replicated storage:** a `CREATE ROLE` on node 1 reaches node 2 without `ON CLUSTER`, and so
  does a `GRANT` (U10). With `ON CLUSTER`, the create fails with `ACCESS_ENTITY_ALREADY_EXISTS`,
  because the second node finds the role the first already wrote.

**An `ON CLUSTER` error can arrive without its code.** An earlier version of
`test_u9_replicated_storage_is_detected_and_reaches_every_node` failed once in three runs of
the cluster suite. The create's error reached the client as a broken HTTP response (urllib3
`IncompleteRead`), so the test's check for "already exists" found no server error. The cause:
- An `ON CLUSTER` statement returns one status row per host, as each host finishes.
  clickhouse-connect's `command` doesn't ask for `wait_end_of_query`, so the server streams
  those rows.
- If a host's error comes before the server has sent anything, the response is an HTTP 500
  with `X-ClickHouse-Exception-Code`.
- If the server has already sent HTTP 200 and the first rows, it can't change the status. It
  appends the error in an `__exception__` block, marked with the `X-ClickHouse-Exception-Tag`
  header's value, and closes the connection without ending the chunked body. `command` reads
  the whole body, so clickhouse-connect raises `OperationalError` with no code.
- `OperationalError` is a `DatabaseError`. So `pytest.raises(DatabaseError)` passed, and only
  the message check failed.
- With `wait_end_of_query=1` the server buffers the response, and the error is always an HTTP
  500 with its code.

By hand on 26.3, with clickhouse-connect 1.10.0, 40 creates of a role that already existed:
- without the setting, 39 answered HTTP 500 with code 493 and 1 broke as above;
- with it, all 40 answered HTTP 500 with code 493.

The test now sends the setting and asserts code 493, `ACCESS_ENTITY_ALREADY_EXISTS`. A second
test forces the race: node 2 starts three seconds late, so node 1's row is always sent
first. By hand, that broke 26 of 26 creates without the setting and none of 14 with it.

**Tests:**
- `test_u9_local_storage_is_detected_and_needs_on_cluster`;
- `test_u9_replicated_storage_is_detected_and_reaches_every_node`;
- `test_u9_on_cluster_error_after_a_host_answered_needs_wait_end_of_query`, with and without
  the setting;
- `test_u10_replicated_grant_reaches_the_other_node_within_the_retry_limit`, for the grant.

**Means for T4 and T5:** confirms 2.5's `ON CLUSTER` rule. With replicated storage, `ON CLUSTER`
is not just unneeded: it fails. Detection reads `system.user_directories`, so the admin needs
`SELECT` on it. T1's limited admin has it through `system.*`. On Cloud the answer is unknown
until T8.

It also adds change 6. `up` already sends `wait_end_of_query=1`, but only on the migration
connection, in `ch_migrate/waiting/waiter.py`. `apply` runs its statements on the admin's
connection, which doesn't get that setting. With local storage and `cluster` set, every
create, grant, revoke and drop runs `ON CLUSTER`. So T5 sends the setting with each of them,
or a failing host's error can't be named, as 2.5's exit 1 requires.

## U10. Reads after writes

**Answer:** with Keeper storage on the Docker cluster, a `GRANT` on node 1 showed in node 2's
`system.grants` within 4 ms. Three runs of 20 grants each gave a median of 1–2 ms and a maximum
of 3–4 ms. The time includes the polling round trip.

**Test:** `test_u10_replicated_grant_reaches_the_other_node_within_the_retry_limit`. It prints
the times and asserts only that each is under 30 s.

**Means for T4:** confirms 2.5's 30 s default for self-hosted Keeper storage. The retry's first
wait can be short. T8 measures Cloud.

## Changes to the design note

Each is applied in `2026-11-11-declarative-access.md`, marked "(T2, Un)". None rewrites a
Part 1 decision. Change 5 bore on Decision 3, which has since been decided there. Where a
change left a choice open, the note now carries the decision, marked "(2026-11-11)".

1. **2.3, grant targets:** a fourth form, `<source>`, for `READ` and `WRITE`, as in
   `READ ON REMOTE`. Core already uses it (2.4). 25.8 and later store the old
   `<SOURCE> ON *.*` spelling as `READ` and `WRITE`, while 24.8 parses only the old spelling.
   `validate` rejects the old spelling and names the new one (2026-11-11) (U1, U8).
2. **2.7:** leaf expansion is the mechanism, summarized there. The candidates list is gone
   (U1, U2).
3. **2.5, `check`:** a partly held group counts as missing, an object that doesn't exist yet
   doesn't, and 24.8 has no `CHECK GRANT` (U3, U5). On a server older than 25.8, `check`
   exits 2 and names its version (2026-11-11).
4. **2.4 and 2.9, cluster grants:** names the four best-effort grants a self-hosted cluster
   needs (U7).
5. **2.5, `apply` order, and 2.7:** narrowing a grant's scope can't grant first (U1). `apply`
   uses one `WITH REPLACE OPTION` of the whole declared set (2026-11-11).
6. **2.5, `ON CLUSTER`:** every `ON CLUSTER` statement is sent with `wait_end_of_query=1`, so
   a host's error keeps its code (U9).

The decisions also added two items to 2.5 that no answer changed: the 25.8 version floor
for the live commands, and `validate`'s bundled list of privilege names.

Two notes record answers that confirm Part 2:
- **2.4:** `system.databases` and `system.tables` are implicit on self-hosted servers, and stay
  required for Cloud (U7).
- **2.5, `ON CLUSTER`:** with replicated storage it fails, and the storage is read from
  `system.user_directories` (U9).

## The spike's questions, decided

T2 left six questions open. They were decided on 2026-11-11, in Decisions 3 and 11 of the
design note.

1. **Scope narrowing in `apply` (T5).** One atomic `GRANT … WITH REPLACE OPTION` of the user's
   or role's whole declared set, so there is no gap. If the admin lacks a right the grantee
   holds, nothing changes and `apply` says why. It is never used on the migration role
   (2026-11-11).
2. **Offline canonical names (T3).** `validate` checks privilege names against a bundled list
   taken from 26.3's `system.privileges`. An unknown name is a warning; `plan` checks names
   against the live server (2026-11-11).
3. **`check` on 24.8 (T6).** The live `access` commands need 25.8 or later. On an older server
   they exit 2 and name its version, so `check` needs no fallback (2026-11-11).
4. **Cluster grants (T6).** U7's four grants stay best-effort, and `check` only warns when one
   is missing (2026-11-11).
5. **`ROLE ADMIN` (a T7 docs item).** The docs say that the smallest admin's `ROLE ADMIN` can
   grant any role to anyone (2026-11-11).
6. **The old source spelling (T3).** `validate` rejects `REMOTE ON *.*` and the other old
   spellings, and its error names the new one, such as `READ, WRITE ON REMOTE`
   (2026-11-11). Recognizing the old spelling still needs the source names, which
   `system.privileges` doesn't mark (step 7 of the comparison).

## Open questions

7. **`bootstrap` on a server older than 25.8 (T6).** Decision 11 covers the live `access`
   commands, and `bootstrap` ends by running `check`, which exits 2 there. Should `bootstrap`
   refuse such a server before its first write? Or should it apply what it can, warn about
   `READ ON REMOTE` under 2.4's best-effort rule, and then report `check`'s exit 2?

   **Superseded 2026-11-16:** every `bootstrap` now exits 2 before its first
   write on a server older than 25.8, legacy mode included. The two tests named below were
   replaced by `test_bootstrap_refuses_24_8_before_any_write`.

   **Decided for T6** (a default, open to review):
   - With an `access.yaml`, `bootstrap` exits 2 before its first write and names the
     version.
   - Without one, it follows Decision 6: it attempts every grant, warns about
     `READ ON REMOTE`, and reports the failures together.
   - It skips the read-back and `check` with a warning, because both need 25.8.
   - Tests: `test_bootstrap_with_access_yaml_refuses_24_8_before_any_write` and
     `test_legacy_bootstrap_on_24_8_attempts_every_grant_and_reports_failures_together`, in
     `tests/integration/test_access_bootstrap.py`. Both run on their own 24.8 container.
