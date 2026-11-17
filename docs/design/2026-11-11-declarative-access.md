# Declarative access for ch-migrate, decoupled from bootstrap

**2026-11-11 · Decisions made on 2026-11-11. Nothing is built yet.**

This note is the design of record for declarative access from T2 on. Code facts in it were
read on `main` at `57ffb8d` (0.8.0) on 2026-11-11. Findings dated 2026-10-29 come from an
earlier proposal for bootstrap permission profiles; where the two differ, this note
wins.

T2's answers to Part 3 are in `2026-11-11-access-spike.md`. The sections they changed say so
with "(T2, Un)". The decisions on the questions T2 left open are marked
"(2026-11-11)".

## Part 1. Decisions (2026-11-11)

1. **A separate command.** `ch-migrate access validate|show|plan|apply|check ENV`. `bootstrap` creates
   the database and the migration identity, then uses the same engine for the migration
   role's grants.
2. **Declarative YAML.** `access.yaml` declares users, roles, role memberships (with default
   roles) and grants (with grant option). `absent: true` drops a user or role.
   - This replaces the earlier proposal's add-on and SQL-grant-file layers.
   - It reverses the 2026-10-29 proposal's rejection of a YAML grant list.
   - The reason: managing users, roles and grants in migrations is tedious and a poor
     experience.
3. **Converge what's declared.**
   - For each user and role the YAML names, `apply` makes its grants and memberships match
     the YAML exactly.
   - No declared user or role is ever left without a grant it keeps (2026-11-11, after
     T2's U1). `apply` grants before it revokes. A change that narrows a grant's scope, such
     as `db.*` to `db.events`, can't grant first, because the server folds the narrower
     grant into the broader one. So it is one atomic `GRANT … WITH REPLACE OPTION` of that
     user's or role's whole declared set. If the admin lacks a right the grantee holds,
     nothing changes and `apply` says why. It is never used on the migration role.
   - Users and roles the YAML doesn't name are never touched.
   - A user or role is dropped only when it's declared `absent: true`.
   - `plan` shows every revoke and drop before `apply` runs them.
4. **The admin applies.** `access apply` runs as `admin_user`. In new projects the migration
   role has no user or role management, so it can't grant itself more.
5. **New projects get core only.** `init` writes an `access.yaml`. The migration role holds
   only the core profile.
6. **Existing projects keep their grants and get clearer reports.** A project with no
   `access.yaml` gets the same grants from `bootstrap` as 0.8 gives. What changes:
   - the role and user are created before any grant;
   - every grant is attempted, and the failures are reported together;
   - a `CURRENT GRANTS` that lands nothing becomes a warning instead of passing silently.

   Config, secret and connection errors still fail as they do today. Apart from those, the
   exit code is non-zero only when a plain `GRANT` fails.
7. **v1 scope.** Users, roles, memberships and grants. Row policies, user settings (such as
   `readonly = 2`) and quotas come later.
8. **Vehicle: ch-migrate 0.9.0** (2026-11-11).
   - It's built on local branches off `main` (0.8.0), one per ticket. Nothing is pushed until
     the whole feature is done and has had a final review for private details.
   - It's released from `main` after T8.
   - This replaces the 2026-10-13 plan, which used 0.9.0 for the 1.0 feature set. That
     release was never tagged, and nothing from `dy/ch-migrate-1.0` is released as 0.9.0.

9. **Build our own narrow solution, with no Terraform requirement** (later the same
   day, after the research below). Porting our approach to the official provider as an
   open-source contribution is a separate, later decision.
10. **Command names**: `access validate` checks the local file offline. `access show`
    connects and reads what the server holds.
11. **After T2's spike** (defaults accepted 2026-11-11):
    - The live `access` commands need ClickHouse 25.8 or later. On an older server they exit 2
      and name its version. 0.8's own `bootstrap` already fails on 24.8. Since 2026-11-16,
      `bootstrap` needs 25.8 too, legacy mode included.
    - `validate` rejects the old source spelling, such as `REMOTE ON *.*`, and its error
      names the new one, `READ, WRITE ON REMOTE`.
    - `validate` checks privilege names offline against a bundled list taken from 26.3's
      `system.privileges`. An unknown name is a warning; `plan` checks names against the
      live server.
    - The four grants a self-hosted cluster needs for `up` stay best-effort, and `check` only
      warns when one is missing (2.9).
    - The docs say that the smallest admin's `ROLE ADMIN` can grant any role to anyone.
12. **After T5's blocker** (2026-11-12):
    - **Best-effort grants on a self-hosted cluster with local storage.** ClickHouse refuses
      `CURRENT GRANTS` with `ON CLUSTER`. Where storage is local and `cluster` is set, `apply`, and
      `bootstrap` with an `access.yaml`, send each best-effort privilege as its own plain
      `GRANT ON CLUSTER`. A full
      admin lands them all on every node; a partial admin lands the ones it holds, and each
      refused one is a warning. Everywhere else, best-effort grants stay `CURRENT GRANTS`.
    - **Source grants compare as one unit.** With `access_control_improvements.enable_read_write_grants`
      off (ClickHouse's default, and not visible from SQL), `READ ON S3` is all-or-nothing and
      reads back as `READ` and `WRITE` after a restart. So `plan` and `apply` treat `READ` and
      `WRITE` on the same source as one grant: declaring either covers both, and nothing revokes
      the other direction. `validate` warns that a one-direction source grant holds only with
      that setting on. A hand-added `WRITE` where `READ` is declared goes unnoticed.
13. **Legacy `bootstrap` stays 0.8 on a cluster** (2026-11-12). With no `access.yaml`,
    `bootstrap` never uses `ON CLUSTER`, even with `cluster` set, and sends best-effort grants
    as `CURRENT GRANTS` on the node it is connected to, exactly as 0.8 does: each node of a
    local-storage cluster is bootstrapped on its own, as today. This keeps Decision 6's
    promise. `bootstrap` with an `access.yaml`, and `access apply`, follow 2.5 and Decision 12.
14. **One node is read, even on a local-storage cluster** (2026-11-12).
    `plan` and `apply` read only the node they connect to. If an `ON CLUSTER` change
    lands on one node and fails on another, that `apply` exits 1 naming the error, but the
    next run against the first node sees nothing to do. v1 documents it: on a local-storage
    cluster, `plan` and `apply` name the node they read, and the README says to run `plan`
    against each node after a failed `ON CLUSTER` apply. Reading every node is a follow-up.
15. **Local and Cloud work equally well** (2026-11-12, T8).
    - **Every replica is read at once.** Where users and roles are replicated and the
      replicas can be read, each read takes every replica through `clusterAllReplicas`, so a
      row missing on every replica is absent with no wait. Replicas that disagree are read
      again until they agree, within `access_read_timeout`; if they never do, the connected
      node's own read counts, and one line names them. Where they can't be read, the
      connected node is read and read again as before, and one line says why (2.5).
      Local-storage clusters don't change (Decision 14).
    - **Which cluster** (2026-11-12, approving T8's rule). `cluster` from
      `config.yaml` always wins. Otherwise, of the clusters that hold this node and list
      another host, those that list the same `(host, port)` set count as one, and the first
      by name is read through; any other case falls back. Why: Cloud with no `cluster` set
      must take the replica read, and its `default` and `all_groups.default` are aliases of
      the same two hosts. "More than one cluster" was meant to catch clusters that disagree,
      not aliases. A stock image's `default` lists only localhost, so it is never a
      candidate: read through, it would agree with itself. The cost: a replicated server
      with one replica falls back on every command until `cluster` is set.
    - **`CLUSTER` granted to every login is held.** ClickHouse Cloud grants `CLUSTER` to
      every login. There the best-effort `CLUSTER ON *.*` is already satisfied: no warning,
      and no try on every `apply`. Legacy `bootstrap` still sends 0.8's statements
      (Decision 13); only what it reports changes.
    - **A create's storage** is the first directory that can take one. Cloud lists `jwt`
      before `replicated`, and a create never goes to `jwt`, `ldap` or `users.xml`.

**Existing tools checked (2026-11-11), and why we don't use them:**
- **ClickHouse's official Terraform provider, `clickhousedbops`.** Ownership is confirmed:
  - its repo is in the `ClickHouse` GitHub org, whose domain is verified as `clickhouse.com`;
  - it is a partner-tier provider on the Terraform Registry;
  - clickhouse.com announced it in a 2025-06-20 blog post;
  - its maintainers are public members of the `ClickHouse` org.

  It covers users, roles, grants, row policies and settings profiles, on Cloud and
  self-hosted. We don't use it for four reasons:
  - **It needs Terraform.**
  - **It can't own a user's or role's whole grant set.** Each grant is a separate resource,
    so a grant added by hand goes unseen. The provider's own proposal for this, ClickHouse/terraform-provider-clickhousedbops#211, has been
    open since 2026-06 with no response.
  - **Partial revokes aren't supported** (ClickHouse/terraform-provider-clickhousedbops#244).
  - **A refresh can silently forget resources** on multi-replica Cloud. When a read briefly
    finds nothing, the provider drops the resource from state with no retry. That bug, ClickHouse/terraform-provider-clickhousedbops#157,
    has been open since 2026-02.

  Its code is still a useful map of the traps; T2 and section 2.7 borrow from it.
- **Atlas** manages ClickHouse users, roles and grants as code, but only in its paid Pro tier.
- **`users.xml`** declares users and grants in server config. Cloud doesn't allow it, and
  ClickHouse recommends SQL-driven access control instead.

**Defaults this note sets.** Any of them can be overridden at review:
- the file's name and place;
- the additive rule for the migration identity (2.2);
- the core profile carries no grant option (2.4);
- legacy mode is chosen because `access.yaml` is missing, not by a config flag (2.4);
- passwords are set only when a user is created;
- a declared user with no `password` is never created: `--sql` leaves it out and names it in
  its header as one that must already exist, and `apply` exits 2 before any change. On 26.3 a
  `CREATE USER` with no `IDENTIFIED` clause is a `no_password` login anyone can use
  (2026-11-11, checked on Docker after T3);
- `--sql` prints each password placeholder unquoted, `IDENTIFIED BY <secret:NAME>`, so SQL run
  unedited fails to parse instead of setting the placeholder text as a password;
- reads after writes (2.5) are retried only where a replica can be behind. Users and roles
  stored on each node's own disk (`local_directory`) are read once; `replicated`, unknown or
  unreadable storage is read again for up to `access_read_timeout` (2026-11-11,
  from T4). Where `replicated` storage's replicas can be read at once, only replicas that
  disagree are read again (Decision 15);
- `~` in `plan` marks every declared user or role that already exists and has a change
  pending. ch-migrate keeps no state, so adoption and drift look the same (from T4);
- `apply` tries every best-effort grant that isn't fully held, on every run, with
  `GRANT CURRENT GRANTS(...)`. That isn't a counted change, and whatever is still missing
  afterwards is a warning. So `plan` can report in sync while it warns about best-effort grants
  (from T4). Where `CURRENT GRANTS` can't run `ON CLUSTER`, Decision 12 applies;
- no renames;
- `access.yaml` wins over migrations.
- **Row policies.** v1 has no row policies, so a new core-only project has nowhere to create
  them except a migration, and that needs `migration_role.grants` to add the row-policy
  rights. Row policies are the first follow-up.

## Part 2. Design

The work is split into eight tickets, T1–T8:
- T1: the test harness;
- T2: a spike on ClickHouse's grant behavior;
- T3: `validate`;
- T4: `show` and `plan`;
- T5: `apply`;
- T6: `bootstrap` and `check`;
- T7: docs;
- T8: acceptance on ClickHouse Cloud.

### 2.1 Three identities

| Identity | Managed by | Declared in |
| --- | --- | --- |
| Admin (`admin_user`) | The operator | `config.yaml`. Never in `access.yaml`; ch-migrate never modifies it. |
| Migration role and migration user | `bootstrap` and `access apply`, additively | The built-in profile, plus `migration_role.grants` in `access.yaml` |
| Every other user and role | `access apply`, converged | `access.yaml` |

### 2.2 The migration identity is additive

- `bootstrap` and `access apply` add any missing profile grants to the migration role. They
  never revoke from it.
- `access plan` lists the role's grants beyond its profile as extras. A person revokes those
  by hand.
- **Why additive:**
  - Two project directories can share one migration role (2.8). If each directory converged
    the role, each would revoke the other's database grants.
  - An existing project that adopts `access.yaml` keeps the 0.8 grants its old migrations
    rely on.
- `access.yaml` can't declare the migration role or user under `roles:` or `users:`. That's a
  validation error. It adds grants through `migration_role.grants`.

### 2.3 `access.yaml`

It sits at the project root, next to `config.yaml`.

```yaml
# Desired users, roles and grants. `ch-migrate access plan ENV` shows how the server differs.
migration_role:
  profile: core          # core (default) | legacy: 0.8's grants, for migrations that create users
  grants:
    - CREATE FUNCTION, DROP FUNCTION ON *.*

roles:
  analyst:
    grants:
      - SELECT, SHOW TABLES ON {db}.*
  loader:
    grants:
      - INSERT, SELECT ON {db}.events
  reporting:
    roles: [analyst]     # a role can hold roles

users:
  dashboards:
    password: {secret: dashboards_password}   # ssm path at environments.<env>.ssm.dashboards_password, else CH_<ENV>_DASHBOARDS_PASSWORD
    roles: [reporting]
  etl:
    password: {secret: etl_password}
    roles: [loader]
    default_roles: [loader]
    grants:
      - SELECT ON {db}.regions

environments:
  production:
    users:
      etl: {absent: true}
```

- **Grants** use ClickHouse's own words: `<privileges> ON <target> [WITH GRANT OPTION]`.
  - A target is `*.*`, `<db>.*` or `<db>.<table>`. Column grants aren't in v1.
  - `READ` and `WRITE` take a source as their target, as in `READ ON REMOTE` (T2, U1 and U8).
    25.8 and later store the old spelling, `REMOTE ON *.*`, as `READ` and `WRITE` on that
    source. `validate` rejects the old spelling, and its error names the new one,
    `READ, WRITE ON REMOTE` (2026-11-11). On 24.8 only the old spelling parses, and
    `READ ON <source>` fails with `SYNTAX_ERROR`; the live `access` commands don't run there
    (2.5).
  - `READ` and `WRITE` on one source, or on `*.*`, compare as one grant (Decision 12). With
    `access_control_improvements.enable_read_write_grants` off, the server holds both. So
    `validate` warns about a user's or role's grant that names one direction alone; the
    built-in profiles aren't checked, because they aren't the user's to change.
  - An entry can also be `{grant: "...", best_effort: true}`. Best-effort means the admin gives
    what it can and ch-migrate warns about the rest. It replaces today's `CURRENT GRANTS`.
- **Placeholders** are `{db}`, `{project}`, `{env}` and `{migration_role}`. They are rendered by
  `sql.render_placeholders`, and every rendered name must pass `validate_identifier`.
- **Secrets.** `{secret: NAME}` resolves the same way the migration password does.
  - If `config.yaml` sets an SSM path at `environments.<env>.ssm.NAME`, that path is read, and
    the environment variable is never checked.
  - Otherwise the environment variable `CH_<ENV>_<NAME>` is read.
  - The caller looks up the path and passes it to `get_secret(env, NAME, ssm_path=...)`, as
    `config.py` and `bootstrap.py` already do. `get_secret` doesn't read `config.yaml` itself.
  - `validate`, `show` and `plan` never resolve a declared user's secret.
- **Memberships.**
  - `roles:` on a user or a role grants it those roles.
  - `default_roles:` picks a subset. Without it, every membership is a default role.
  - A membership can name a role the YAML doesn't declare, if that role exists on the server.
    If it doesn't exist, `plan` names it and exits 2.
- **Environments.** `environments.<env>` deep-merges over the base. Mappings merge and lists
  replace.
- **`absent: true`** drops the user or role. Set in an environment block, it drops it in that
  environment only.
- **Validation.** Unknown keys are errors. Each error names the file, the key path and the
  reason.

### 2.4 Built-in profiles

The profiles ship as packaged YAML in `ch_migrate/access/profiles/`. Keeping them out of
Python strings also keeps the python-style gate quiet.

- **`core`** is what the migration role needs in a new project. T2 (U6, U7) confirmed this
  split on a single node: core required alone runs every ch-migrate path the spike tried, and
  the smallest admin gives every required grant. On a self-hosted cluster, four of the best-effort grants
  are needed (see 2.9). The profile:
  - **Required, on `{db}.*`, without grant option:**
    - `CREATE TABLE`, `DROP TABLE`, `UNDROP TABLE`, `ALTER`;
    - `CREATE VIEW`, `DROP VIEW`, `CREATE DICTIONARY`, `DROP DICTIONARY`;
    - `SELECT`, `INSERT`, `DELETE`, `TRUNCATE`, `OPTIMIZE`;
    - `SHOW TABLES`, `SHOW COLUMNS`, `SHOW DICTIONARIES`.
  - **Required:** `SELECT` on `system.databases`, `system.tables`, `system.mutations` and
    `system.dictionaries`.
    - `introspect.py` and `scaffold.py` read `system.dictionaries`.
    - Today only the `system.*` blanket covers it.
    - Self-hosted servers grant `system.databases` and `system.tables` implicitly. They stay
      required for Cloud, which T8 checks.
  - **Best-effort:**
    - `SELECT` on `system.parts` and `system.replicas`;
    - `SYSTEM SYNC REPLICA` and `SYSTEM SYNC DATABASE REPLICA` on `{db}.*`;
    - `CLUSTER`, `READ ON REMOTE`, and `CREATE TEMPORARY TABLE ON *.*`.
  - **No grant option anywhere.** In a new project, rights for other roles come from
    `access.yaml`, which the admin applies.
- **`legacy`** is 0.8's migration role exactly. It is a standalone copy of the grant
  statements in 0.8's `build_bootstrap_sql`, not "core plus extras". Core differs from 0.8 in
  ways an extras list wouldn't undo:
  - core adds `SELECT ON system.dictionaries`, which 0.8 never grants;
  - core drops grant option;
  - core makes `CREATE TEMPORARY TABLE` best-effort.

  T1's characterization fixture defines this profile. For orientation only, 0.8 grants core's
  database rights `WITH GRANT OPTION`, plus the following:
  - on `*.*`:
    - `CREATE FUNCTION`, `DROP FUNCTION`;
    - `CURRENT GRANTS(CREATE USER, ALTER USER, DROP USER)`;
    - `CREATE ROLE`, `ALTER ROLE`, `DROP ROLE` and `ROLE ADMIN`;
    - `CREATE TEMPORARY TABLE`, as a plain grant with grant option;
  - `ACCESS MANAGEMENT` and the row-policy rights on `{db}.*`;
  - `CURRENT GRANTS(SELECT ON system.*)` and `SELECT ON system.grants`.
- **Best-effort grants** follow three rules:
  1. `validate --sql`, `apply` and `bootstrap` emit them as `GRANT CURRENT GRANTS(...)`. Where the admin
     holds part of a privilege, that still lands the part. A plain `GRANT` would land nothing.
     ClickHouse refuses `CURRENT GRANTS` with `ON CLUSTER`, so where statements run `ON CLUSTER`
     each privilege is its own plain `GRANT ON CLUSTER`, and a refused one is a warning
     (Decision 12).
  2. A best-effort grant that is missing or only partly held is a warning in `plan`, `apply`,
     `check` and `bootstrap`, and never changes the exit code. Otherwise `plan` could never
     reach 0 where the admin can't give it, such as on ClickHouse Cloud. A right the server
     grants every login is held without a grant: on Cloud, and wherever
     `on_cluster_queries_require_cluster_grant` is off, that is `CLUSTER ON *.*`. The
     admin's `CHECK GRANT CLUSTER ON *.*` answers 1 there while neither its own grants nor
     its enabled roles' hold `CLUSTER`, which is how `plan` tells (Decision 15). `check`
     already agrees, since `CHECK GRANT` answers 1 for the migration user too.
  3. Only a missing required grant makes `plan` or `check` exit 1.
- **Legacy mode** applies to a project with no `access.yaml`.
  - `bootstrap` applies the `legacy` profile.
  - `mcp_user_name` and `dict_reader_name` produce the same read-only and dictionary roles and
    users that 0.8 creates. These stay additive: 0.8 never converged them.
  - `access apply` in legacy mode only adds missing `legacy` grants to the migration role.

### 2.5 Commands

| Command | Logs in as | Secrets it needs | Writes | Exit codes |
| --- | --- | --- | --- | --- |
| `access validate ENV` | Nobody | None | Nothing | 0 valid, 2 invalid |
| `access show ENV [NAME...]` | Admin | Admin's | Nothing | 0. 2 on an error. |
| `access plan ENV` | Admin | Admin's | Nothing | 0 in sync, 1 changes pending, 2 error |
| `access apply ENV [--reset-passwords]` | Admin | Admin's, plus those of users it creates | Yes | 0 converged (best-effort shortfalls warn), 1 not converged: a change failed or was skipped (each one named), or something still differs, 2 error before any change |
| `access check ENV` | Migration user | Migration user's | Nothing | 0 every required grant held, 1 missing (each one named), 2 error |
| `bootstrap ENV [--dry-run]` | Admin, then the migration user for the check | Admin's and the migration user's. None with `--dry-run`. | Yes | Same as `apply` |

- **Server version.** The live commands, `show`, `plan`, `apply` and `check`, need ClickHouse
  25.8 or later. On an older server they exit 2 and name its version (2026-11-11). 24.8
  has no `CHECK GRANT` and no `READ ON <source>` (T2, U5 and U8).
- **`validate`** checks `access.yaml` and the built-in profiles, and reports every error with
  the file, the key path and the reason.
  - With `--sql`, it also prints the SQL a DBA can review and run: `CREATE ROLE IF NOT EXISTS`,
    `CREATE USER IF NOT EXISTS` and the `GRANT` statements. Each password is replaced by a
    placeholder that names its secret.
    - A declared user with no `password` is never created: `--sql` leaves it out and names it
      in its header as one that must already exist, and `apply` exits 2 before any change. On
      26.3 a `CREATE USER` with no `IDENTIFIED` clause is a `no_password` login anyone can use.
    - `--sql` prints each password placeholder unquoted, `IDENTIFIED BY <secret:NAME>`, so SQL
      run unedited fails to parse instead of setting the placeholder text as a password.
  - It needs no server and no secrets. It reads `config.yaml` through a new loader that
    resolves no secrets.
  - It checks privilege names against a bundled list taken from 26.3's `system.privileges`.
    An unknown name is a warning; `plan` checks names against the live server
    (2026-11-11).
  - `get_env_config` keeps its shape, because client code imports it.
- **`show`** connects as the admin and prints what the server holds, in `access.yaml` form.
  - It covers the declared users and roles, or the names given on the command line.
  - It never shows passwords.
  - Pointed at existing users, its output is the starting point for adopting them, so it
    replaces the "export" follow-up.
- **`plan`** prints one line per change, with `ui` markers:
  - `+` create or grant;
  - `-` revoke or drop;
  - `~` adopt: the entity exists and is now managed;
  - `!` an extra grant on the migration role. These don't change the exit code.

  `--json` is a follow-up.
- **`apply`** runs its changes in this order:
  1. create roles;
  2. create users;
  3. memberships;
  4. grants, then the replaces that narrow a scope (below);
  5. default roles;
  6. revokes;
  7. drops.

  It continues past a failed statement, then plans again and reports whatever still differs.
  - **Narrowing a scope can't grant first** (T2, U1). For `SELECT ON db.*` to
    `SELECT ON db.events`, the new grant is absorbed and writes nothing, so revoking `db.*`
    afterwards removes both. So `apply` runs one atomic `GRANT … WITH REPLACE OPTION` of
    that user's or role's whole declared set (2026-11-11; Decision 3).
    - There is no gap without access, and role memberships are kept.
    - It needs the admin to hold every right the grantee has now, with grant option. If the
      admin lacks one, the statement fails, nothing changes, and `apply` says why.
    - It is never used on the migration role, which is additive (2.2).
  - **Secrets first.** Before its first write, `apply` resolves every secret it will need,
    SSM lookups included. A missing secret exits 2 with nothing changed.
  - **No revoke without its replacement.** Revokes run only if every earlier create,
    membership, grant, replace and default-role change succeeded, for any user or role. If
    one failed, every revoke is skipped and reported, so old access survives. The skip names
    the user's or role's own failure, or a role's it holds, when there is one; otherwise the
    failure elsewhere. A failure anywhere counts because a role's holders may rely on a
    sibling role whose change failed: a user holding `A` and `B`, with access moving from `A`
    to `B`, keeps `A` when the grant to `B` fails.
    - Example: a user moves from role A to role B, and granting B fails. `apply` keeps A.
    - A failed change to a role counts as failed for every declared user or role that holds
      it, directly or through declared roles. Example: the user is granted B, but B's own
      grant fails: the user keeps A.
    - A replace (`WITH REPLACE OPTION`) is gated like a revoke: it is skipped if any earlier
      change failed. It keeps whatever is
      held inside a declared best-effort grant's scope.
    - A default-role change is gated the same way, so a role the user keeps stays active at
      login.
    - Replaces run dependencies first: a role's replace runs before the replace of any user
      or role that holds it, directly or through declared roles, whatever access.yaml's
      order, so the gate sees how it went.
    - A replace that can't keep exactly what is held of a declared best-effort grant isn't
      sent: it is skipped and named, the user or role keeps what it holds, and `apply` exits
      1, since the replace is still pending. That is a revoke inside the grant's scope, such
      as `SELECT ON system.*` held less `system.users`, or a revoke of only the grant option
      there where the declared grant carries one: one `WITH REPLACE OPTION` statement can't
      write "all but". Every revoke, replace and default-role change after it is skipped as
      after a failure.
    - Drops run only if no earlier change failed and every replace was sent. Otherwise each
      drop is skipped and named: a revoke held back may leave a user or role relying on a
      role about to be dropped.
  - A password is a bound parameter sent in the request body (`raw_query`, form-encoded),
    never in the statement or the URL, where a proxy's log or a connection error could show
    it. Every error ch-migrate prints hides each secret it has read, raw and URL-encoded.
  - Passwords are set only when a user is created. `--reset-passwords` sets them again for the
    declared users, including one this run creates, right after its create: if someone else
    makes the user between the read and `CREATE USER IF NOT EXISTS`, the create keeps their
    password. A failed reset holds back every revoke, replace and
    default-role change, as any failure does, and every later change to that user: its
    password may not be the declared one. A failed create does the same: an admin without
    `CREATE USER` fails it even when someone else has made the user.
  - It refuses to declare or modify `admin_user`, or the login it is connected as.
- **Reads after writes, for `apply`, `plan`, `show` and `bootstrap`.**
  - On multi-replica Cloud, a grant just made may not show up yet on a read routed to the
    other replica. A missing row never leads straight to a create or a revoke.
  - This is the gap behind the official provider's bug ClickHouse/terraform-provider-clickhousedbops#157.
  - **Every replica at once** (Decision 15). Where users and roles are replicated, each read
    takes every replica in one query per table, through `clusterAllReplicas`, with each row
    tagged by its replica's `hostName()`, and with `skip_unavailable_shards = 0`, so a
    replica that can't be reached fails the read instead of reading as empty.
    - The cluster is `cluster` from `config.yaml` whenever it is set. Otherwise it is the
      cluster in `system.clusters` that holds this node and at least one other host;
      clusters that list the same `(host, port)` set count as one (Decision 15). Cloud lists
      `default` and `all_groups.default` with the same hosts, and a stock image lists a
      `default` of localhost alone, which isn't one.
    - The read is settled when every replica returns the same rows for the names read, so a
      row missing on every replica is absent at once. That is safe right after a write:
      over 30 rounds each of `CREATE ROLE`, `GRANT`, `REVOKE` and `DROP ROLE`, every replica
      showed the write on the very next read, 2 of 2 on Cloud 26.6 and 2 of 2 on the local
      Keeper cluster (T8). An earlier probe saw replica lag of 0.070 s at
      most, about 0.04 s at p50.
    - Replicas that disagree are read again with backoff, within the time limit below, and
      each such read also reads the node connected to. If they still disagree at the
      deadline, as a cluster that takes in a node outside the replicated storage always
      would, no row counts as present or absent from them: the connected node's own read
      counts, as at the deadline of the fallback below, and one line names the replicas
      (2026-11-12).
    - `clusterAllReplicas` needs `READ ON REMOTE`, on 25.8, 26.3 and Cloud 26.6, plus
      `SELECT` on each table it reads, which the read needs anyway; on Cloud a login without
      that `SELECT` is told it lacks `SHOW COLUMNS` on the table. `CREATE TEMPORARY TABLE`
      isn't needed. Finding the cluster also reads `system.clusters`. A login without these
      takes the fallback below; it never fails.
  - **Falling back.** When the replicas can't be read that way (no one cluster found, a
    login without `READ ON REMOTE`, a replica that can't be reached), the command reads the
    node it is connected to. A read that finds nothing is retried with backoff, up to a time
    limit (default 30 s, configurable), before it counts as "absent". A lone replicated node
    keeps this wait: it cannot be distinguished from a node whose peers aren't listed in a
    cluster. The way to read is chosen at the first read and kept for the command.
  - The wait notice and fallback reason print only when the command actually waits. The
    fallback reason prints once per command, even if `apply` waits only after its writes.
- **`ON CLUSTER`.** On a self-hosted cluster, users and roles live either in replicated
  storage (Keeper) or locally on each node.
  - ch-migrate reads which one from the server.
  - With local storage and `cluster` set in `config.yaml`, every create, grant, revoke and drop
    runs `ON CLUSTER`. A best-effort grant goes one privilege per `GRANT ON CLUSTER`, because
    `CURRENT GRANTS` can't run there (Decision 12).
  - The cluster's name is written backquoted, so a name such as `c-1` parses (checked on
    25.8 and 26.3).
  - With replicated storage, or on Cloud, it doesn't. With replicated storage, `ON CLUSTER`
    fails with `ACCESS_ENTITY_ALREADY_EXISTS` (T2, U9).
  - `bootstrap` with no `access.yaml` never does, even with `cluster` set: it runs on the node
    it is connected to, as 0.8 does, and each node is bootstrapped on its own (Decision 13).
  - `plan`, `apply` and `bootstrap` with an `access.yaml` read only the node they are
    connected to (Decision 14). With local storage and `cluster` set, each prints one line
    naming that node, by the server's `hostName()`, or by the configured host and port when
    that can't be read. The line says to run `plan` against each node after a failed
    `ON CLUSTER` apply. Nothing prints on Keeper storage, on a single node, or for legacy
    `bootstrap` (T6).
  - The first storage in `system.user_directories`, by `precedence`, that a create can go
    to is where it goes: `local_directory`, `replicated` or `memory` (T2, U9). `users_xml`,
    `ldap` and Cloud's `jwt` only read; Cloud lists `jwt` before `replicated` (T8).
  - Every `ON CLUSTER` statement is sent with the setting `wait_end_of_query=1`, as `up`'s
    waiter already does. Without it, a host that fails after another has answered can't
    turn the response into an error: the server has already sent HTTP 200 and the first
    rows, so it cuts the response, and the client gets no error code (T2, U9).
- **`check`** runs `CHECK GRANT`, as the migration user, for each required grant of the
  migration role (its profile plus `migration_role.grants`). It is for DBA-managed servers
  where ch-migrate has no admin login.
  - A group that is only partly held answers 0, so it counts as missing. A grant on an object
    that doesn't exist yet answers 1 (T2, U3 and U5).
  - `CHECK GRANT` on a group checks every flag in it, even those its target can't hold.
    `ACCESS MANAGEMENT ON db.*` answers 0 on 25.8 and 26.3 right after 0.8's `bootstrap`
    grants it, because the group's user and role rights apply only on `*.*`. So `check` sends
    each grant as the flags it means on its target (2.7), read from the server's own
    `system.privileges`, which the migration user can read. Every grant of both profiles
    then answers 1 after a full admin's `bootstrap` (T6).
  - `CHECK GRANT` takes no `WITH GRANT OPTION`, so `check` checks privileges only.
  - 24.8 has no `CHECK GRANT`; it reads the statement as `CHECK TABLE` and fails with
    `SYNTAX_ERROR` (T2, U5). So `check`, like every live `access` command, exits 2 on a
    server older than 25.8 and names its version (2026-11-11).
- **`bootstrap`** does the following, in order:
  1. `CREATE DATABASE IF NOT EXISTS`;
  2. creates the migration role and user if they're missing, with the password set at
     creation;
  3. adds the migration role's profile grants;
  4. in legacy mode, creates the read-only and dictionary roles and users;
  5. runs `check`.

  It doesn't apply the rest of `access.yaml`. A fresh environment runs `bootstrap`, then `up`,
  then `access apply`.

### 2.6 Living alongside migrations

- **New projects.** The migration role has no user or role management, so a migration can't
  create users or roles. They go in `access.yaml`.
- **Projects whose migrations create users or roles** keep working:
  - In legacy mode, nothing changes.
  - A project that adopts `access.yaml` sets `migration_role.profile: legacy` to keep those
    rights on fresh environments.
- **When a migration and `access.yaml` both touch a declared user or role, `access.yaml` wins at
  the next `apply`.** Deploys run `up`, then `access apply`. `access plan` in CI shows any drift
  a migration introduced.
- **Adoption.** Declaring a user or role that already exists adopts it. `plan` shows `~ adopt`,
  then the grant and membership differences. Its password isn't touched unless you pass
  `--reset-passwords`.
- **Renames aren't supported.** Declare the old name `absent: true` and add the new one.

### 2.7 Comparing desired and live grants

`plan` must compare without writing. The constraints, partly learned from the official
provider's code:
- **A text diff of `SHOW GRANTS` doesn't work.** ClickHouse folds and expands privileges
  (2026-10-29 finding).
- **A parent privilege may be stored as its children.** `CREATE` or `ACCESS MANAGEMENT` can
  appear in `system.grants` as one row per child privilege.
- **A narrower grant can be absorbed by a broader one,** and then ClickHouse writes no row for
  it.
- **Prefix grants** (`db.t*`) are stored without the `*` before ClickHouse 26.7. v1 doesn't
  allow prefix targets.
- **Only canonical names round-trip.** The loader rewrites aliases to canonical privilege
  names, using `system.privileges`, before comparing.
- **Narrowing a grant must revoke only the difference.** `REVOKE ALTER` also removes
  `ALTER UPDATE`, so going from `ALTER` to `ALTER UPDATE` can't be "grant the new, revoke the
  old".

**The mechanism is leaf expansion** (T2, U1 and U2). It is read-only and needs no
version-specific list. The spike record's "The comparison" section has the steps:
- Read `system.privileges` from the server being compared. The three tested images differ.
- A privilege with a `level` carries its own flag; one without is a group. A privilege means
  its own flag plus its descendants' flags, kept only where the target's scope allows that
  level.
- Expand desired grants and `system.grants` rows to rights: one per flag, scope and grant
  option. A row with `is_partial_revoke = 1` is a revoke.
- Compare by coverage both ways. A right is missing when no live right covers it, and extra
  when no desired right covers it. A revoke row on a declared user or role is drift; granting
  the declared grant again clears it.
- Narrow a privilege by revoking the extra flags on their own scope. Drop only a grant option
  with `REVOKE GRANT OPTION FOR`. Narrowing a scope is different; see `apply` in 2.5.
- A declared `READ` or `WRITE` on a source, or on `*.*`, covers the other direction on the
  same scope, so that one is never extra (Decision 12). The declared direction must still be
  held: a role that declares `READ` and holds only `WRITE` is granted `READ`.
- A column grant on a live user or role is narrower than its table. v1 declares none, so a
  desired table grant shows it as missing, and granting the table absorbs the column row.

T2 also weighed a scratch role and `CHECK GRANT`. A scratch role writes, so `plan` can't use
it. `CHECK GRANT` sees only the current user and no grant option, so only `check` uses it.

### 2.8 A second database

- **This already works on 0.7.0** (verified in Docker 26.3 on 2026-10-29):
  - Two project directories each get their own database and version table.
  - They share the migration role when both use the same `project.name` and `migration_user`.
- **v1 adds an optional `migration_role:` key in `config.yaml`.** The second directory names
  the role it shares instead of relying on a matching project name.
- **Under the additive rule (2.2):**
  - Each directory's `bootstrap` adds core on its own database. Neither revokes the other's
    grants.
  - On a server where only the first directory was bootstrapped, the role has no rights on
    the second database, so a stray migration there fails with `ACCESS_DENIED`.

### 2.9 Not in v1

- row policies, user settings and settings profiles, quotas, and host restrictions;
- column-level grants;
- `--json`;
- prefix grant targets (`db.t*`);
- revoking from the migration role;
- renames;
- a lint rule for user and role statements in migrations;
- cluster grants that become required only when a cluster applies. On a self-hosted cluster,
  `up` needs `CLUSTER`, `READ ON REMOTE`, `SYSTEM SYNC REPLICA` and, while a mutation on a
  replicated table is pending, `SELECT ON system.replicas` (T2, U7). Core keeps them
  best-effort, and `check` only warns when one is missing (2026-11-11);
- a read-only login for `plan`. It needs the admin's for now;
- reading every node of a local-storage cluster, so that an `ON CLUSTER` change that failed on
  one node is seen and made on the next run (Decision 14).

## Part 3. Unknowns T2 settles

Each answer becomes a test in `tests/integration/test_access_spike.py` that keeps asserting
it, plus a line in `docs/design/2026-11-11-access-spike.md` that names the test.

- **U1. Read-only comparison.** Does leaf expansion from `system.privileges` reproduce what
  `system.grants` holds after each `GRANT` in both profiles? It must handle:
  - folded privileges (`DELETE`, `ACCESS MANAGEMENT`);
  - a parent stored as its children;
  - a narrower grant absorbed by a broader one, which leaves no row;
  - narrowing;
  - aliases rewritten to canonical names.

  If no read-only mechanism works, stop and decide again.
- **U2. Partial revokes.** How do they appear in `system.grants`, and does granting again
  clear them?
- **U3. Objects that don't exist yet.** Does ClickHouse accept grants on tables and databases
  that don't exist? The answer decides whether `access apply` can run before `up`.
- **U4. Passwords.** Confirm that nothing the admin can read by default shows a password or
  its hash. Then `plan` never compares passwords.
- **U5. `CHECK GRANT`.** Which of images 24.8, 25.8 and 26.3 support it, and what does it
  return for a partly granted privilege?
- **U6. The smallest admin.** What is the smallest admin grant set that can run a core-only
  `bootstrap` on single-node Docker? Which core grants can that admin not give?
- **U7. What ch-migrate needs.** Which core grants does each ch-migrate path need, on a single
  node and on the replicated cluster fixture? The answer moves grants between required and
  best-effort.
- **U8. `system.privileges`.** Is it there, and in what shape, on the same images as U5?
- **U9. Where users and roles are stored.** On the cluster fixture, run the cluster once with
  local access storage and once with replicated (Keeper) storage. Answer two questions:
  - Can ch-migrate tell which mode the server uses, from the server alone?
  - With local storage, does a create or grant without `ON CLUSTER` reach only one node, while
    one with `ON CLUSTER` reaches every node?
- **U10. Reads after writes.** With replicated storage on the cluster fixture, how long after
  a `GRANT` on one node until another node's `system.grants` shows it? This sets the retry
  default. T8 measures the same on Cloud.
