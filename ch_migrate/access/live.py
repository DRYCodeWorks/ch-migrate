"""Read users, roles, grants and memberships as the admin; SQL is in sql/live.sql.

Use the first creatable storage, never a read-only directory. Local disk is read once.
For replicated storage, read every replica through the configured or unambiguous cluster.
Agreement settles the read; disagreement retries within a deadline, then the connected
node counts. Unavailable replicas fall back to that node's timed missing-row retry.
A fallback reason is emitted once per command, only before an actual wait.
Implicit CLUSTER is detected independently of stored grant rows. See design 2.5.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping

from ch_migrate.access.model import Grant, Target
from ch_migrate.access.privileges import PrivilegeTree, tree_from_document
from ch_migrate.access.rights import Holdings, Right, rights_of, stored_rows
from ch_migrate.access.server import server_errors, server_message
from ch_migrate.access.settings import READ_TIMEOUT
from ch_migrate.access.templates import statement

# `system.user_directories` types: each node's own disk, and Keeper.
LOCAL_STORAGE = "local_directory"
REPLICATED_STORAGE = "replicated"

_KINDS = ("user", "role")
# What one read takes, one query each.
_TABLES = ("users", "roles", "grants", "role_grants")
# A replica that can't be reached fails the read; it never reads as one that holds nothing.
_EVERY_REPLICA = {"skip_unavailable_shards": 0}
# What some servers grant every login: CLUSTER, where ON CLUSTER needs no grant.
_CLUSTER = Grant(("CLUSTER",), Target())


@dataclass(frozen=True)
class Membership:
    """A role that a user or role holds."""

    role: str
    default: bool = True
    admin_option: bool = False


@dataclass(frozen=True)
class LiveEntity:
    """One user or role on the server: its grant rows and the roles it holds.

    A role granted to a user whose default roles are `ALL` becomes a default role at once;
    `default_all` and `default_except` say whether that is so.
    """

    holdings: Holdings = Holdings()
    memberships: tuple[Membership, ...] = ()
    default_all: bool = True
    default_except: tuple[str, ...] = ()


@dataclass(frozen=True)
class LiveState:
    """One read of the server: its privilege tree, and the users and roles asked about."""

    tree: PrivilegeTree
    # Where a create is stored: `local_directory`, `replicated`, or None when unreadable.
    storage: str | None = None
    users: Mapping[str, LiveEntity] = field(default_factory=dict)
    roles: Mapping[str, LiveEntity] = field(default_factory=dict)
    # Of the data sources asked about, those the server takes as a grant target.
    sources: frozenset[str] = frozenset()
    # The privileges the server grants every login on `*.*`, such as CLUSTER on Cloud.
    granted_to_all: frozenset[str] = frozenset()
    # Whether every replica returned the same rows; None where only one node was read.
    agreed: bool | None = None
    # Where they didn't: every replica read. Then the rows are the node connected to's own.
    disagreeing: tuple[str, ...] = ()

    def find(self, kind: str, name: str) -> LiveEntity | None:
        """The user or role with this name, if the read found it."""
        return (self.users if kind == "user" else self.roles).get(name)

    def held_by_all(self, right: Right) -> bool:
        """Whether every login holds `right` without a grant, as CLUSTER ON *.* on Cloud."""
        return (
            right.flag in self.granted_to_all and right.scope.depth == 0 and not right.grant_option
        )

    def behind(self, unsettled: Callable[[LiveState], bool]) -> bool:
        """Whether to read again: replicas that disagree, or, where one node was read and
        it may lag, a row that `unsettled` finds missing."""
        if self.agreed is not None:
            return not self.agreed
        return self.may_lag and unsettled(self)

    @property
    def may_lag(self) -> bool:
        """Whether a read can be behind a recent write: anything but a node's own disk."""
        return self.storage != LOCAL_STORAGE


@dataclass(frozen=True)
class Wanted:
    """What to read: users and roles by name, and the data-source names to check."""

    names: tuple[str, ...]
    sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class Patience:
    """How long to keep reading again before a missing row counts as absent."""

    seconds: float = READ_TIMEOUT
    first_wait: float = 0.05
    longest_wait: float = 2.0
    sleep: Callable[[float], None] = time.sleep
    # The clock the time limit is measured on. It runs while a read does, too.
    clock: Callable[[], float] = time.monotonic
    # Called once, with `seconds`, before the first wait.
    on_wait: Callable[[float], None] = lambda seconds: None


@dataclass
class Reader:
    """Reads what the server holds for one command, as `client`'s login. It only reads.

    How it reads is settled at its first read and kept for the command: every replica
    through a cluster, or the node connected to. A fallback is announced through
    `on_fallback` only before an actual wait, once per command. Replicas that still
    disagree at the deadline are named through `on_disagreement`.
    """

    client: Any
    patience: Patience = field(default_factory=Patience)
    cluster: str | None = None  # config.yaml's `cluster`, when it is set
    on_fallback: Callable[[str], None] = lambda reason: None
    on_disagreement: Callable[[tuple[str, ...]], None] = lambda replicas: None
    _through: str | None = field(default=None, init=False, repr=False)
    _chosen: bool = field(default=False, init=False, repr=False)
    _fallback_reason: str | None = field(default=None, init=False, repr=False)

    def settled(self, wanted: Wanted, unsettled: Callable[[LiveState], bool]) -> LiveState:
        """Read, and read again while a replica may be behind (`read_settled`).

        Replicas that still disagree at the deadline are named, and the last read, of the
        node connected to, counts.
        """
        patience = replace(self.patience, sleep=self._sleep)
        state = read_settled(lambda: self.read(wanted), unsettled, patience)
        if state.disagreeing:
            self.on_disagreement(state.disagreeing)
        return state

    def read(self, wanted: Wanted) -> LiveState:
        """One read of everything `wanted` names: of every replica, where they can be read."""
        client = self.client
        with server_errors():
            tree = read_tree(client)
            storage = _storage(client)
            rows = self._rows(tree, storage, {"names": list(wanted.names)})
            return LiveState(
                tree=tree,
                storage=storage,
                users=rows.entities("user"),
                roles=rows.entities("role"),
                sources=_accepted_sources(client, wanted.sources),
                granted_to_all=_granted_to_all(client, tree),
                agreed=rows.agreed,
                disagreeing=rows.disagreeing,
            )

    def _rows(self, tree: PrivilegeTree, storage: str | None, names: dict[str, Any]) -> _Rows:
        from clickhouse_connect.driver.exceptions import DatabaseError

        cluster = self._replicas_through(storage)
        if cluster is not None:
            try:
                return _every_replica(self.client, tree, {**names, "cluster": cluster})
            except (DatabaseError, _NoReplica) as error:
                message = server_message(error) if isinstance(error, DatabaseError) else error
                self._fall_back(f"Reading every replica of cluster {cluster} failed: {message}")
        return _node_rows(self.client, tree, names)

    def _replicas_through(self, storage: str | None) -> str | None:
        """The cluster every replica is read through, or None to read one node."""
        if not self._chosen:
            self._chosen = True
            if storage == REPLICATED_STORAGE:
                self._through = self.cluster or self._discovered()
        return self._through

    def _discovered(self) -> str | None:
        """The cluster that holds this node and other hosts, or None with a fallback reason."""
        from clickhouse_connect.driver.exceptions import DatabaseError

        try:
            rows = _query(self.client, "clusters")
        except DatabaseError as error:
            self._fall_back(f"Can't read system.clusters: {server_message(error)}")
            return None
        candidates = [row for row in rows if len(row["hosts"]) > 1]
        by_hosts: dict[tuple[str, ...], str] = {}
        for row in candidates:
            by_hosts.setdefault(tuple(row["hosts"]), str(row["cluster"]))
        if len(by_hosts) == 1:
            return next(iter(by_hosts.values()))
        found = (
            f"Clusters {_and(sorted(str(row['cluster']) for row in candidates))} hold this "
            "node and list different hosts"
            if by_hosts
            else "No cluster in system.clusters holds this node and another host"
        )
        advice = (
            "Set `cluster` in config.yaml to the cluster whose nodes share these users and roles."
            if by_hosts
            else "If other nodes share these users and roles, set `cluster` in config.yaml "
            "to a cluster that lists them."
        )
        self._fall_back(f"{found}. {advice}")
        return None

    def _fall_back(self, reason: str) -> None:
        self._through = None
        self._fallback_reason = reason

    def _sleep(self, seconds: float) -> None:
        """Announce a pending fallback only when a missing row really makes us wait."""
        if self._fallback_reason is not None:
            self.on_fallback(self._fallback_reason)
            self._fallback_reason = None
        self.patience.sleep(seconds)


def read_settled(
    read: Callable[[], LiveState], unsettled: Callable[[LiveState], bool], patience: Patience
) -> LiveState:
    """Read, and while a replica may be behind, read again (`LiveState.behind`).

    The waits double from `first_wait` up to `longest_wait`, and none runs past a deadline
    `patience.seconds` after the first read starts, on `patience.clock`. The reads count
    toward it as well as the waits, so slow reads can't stretch it. One last read may start
    at the deadline, so the last wait can see the replicas agree. A read is several
    requests, and each is bounded by the driver's request timeout, not the read as a whole.
    Once it has passed, the last read counts as it is.
    """
    deadline = patience.clock() + patience.seconds
    state, wait, announced = read(), patience.first_wait, False
    while (left := deadline - patience.clock()) > 0 and state.behind(unsettled):
        if not announced:
            patience.on_wait(patience.seconds)
            announced = True
        patience.sleep(min(wait, left))
        wait = min(wait * 2, patience.longest_wait)
        state = read()
    return state


def read_live(client: Any, wanted: Wanted) -> LiveState:
    """One read of everything `wanted` names, as the login `client` holds. It only reads."""
    return Reader(client).read(wanted)


def read_tree(client: Any) -> PrivilegeTree:
    """The server's own `system.privileges`: the images differ, so no bundled list is used.

    Any login can read it, the migration user included.
    """
    rows = _query(client, "privileges")
    return tree_from_document({"privileges": rows, "source_spellings": {}})


def read_node(client: Any) -> str | None:
    """The server's `hostName()`: the node `client` reads. None when the server won't say."""
    from clickhouse_connect.driver.exceptions import ClickHouseError

    try:
        rows = _query(client, "node")
    except ClickHouseError:
        return None
    return str(rows[0]["name"]) if rows else None


@dataclass(frozen=True)
class _Rows:
    """The rows one read returned, before they are sorted by user and role."""

    tree: PrivilegeTree
    existing: Mapping[str, list[dict[str, Any]]]  # by kind: the rows of system.users or .roles
    grants: list[dict[str, Any]]
    memberships: list[dict[str, Any]]
    agreed: bool | None = None  # whether every replica returned these rows; None: one node
    disagreeing: tuple[str, ...] = ()  # where they didn't, every replica read

    def entities(self, kind: str) -> dict[str, LiveEntity]:
        return {row["name"]: self._entity(kind, row) for row in self.existing[kind]}

    def _entity(self, kind: str, row: dict[str, Any]) -> LiveEntity:
        column, name = f"{kind}_name", row["name"]
        stored = tuple(
            flag_row
            for grant in self.grants
            if grant[column] == name
            for flag_row in stored_rows(self.tree, grant)
        )
        held = (_membership(held) for held in self.memberships if held[column] == name)
        return LiveEntity(
            Holdings(stored),
            tuple(sorted(held, key=lambda membership: membership.role)),
            default_all=bool(row.get("default_roles_all", True)),
            default_except=tuple(row.get("default_roles_except", ())),
        )


@dataclass(frozen=True)
class _Copies:
    """One table as every replica returned it: each row once, and which replicas hold it."""

    rows: Mapping[str, dict[str, Any]]  # by the row's text
    held: Mapping[str, frozenset[str]]  # by replica: the text of each row it returned

    @property
    def agreed(self) -> bool:
        return len(set(self.held.values())) <= 1

    def ordered(self) -> list[dict[str, Any]]:
        """Each distinct row, ordered by its text."""
        return [self.rows[text] for text in sorted(self.rows)]


class _NoReplica(Exception):
    """A read of every replica that no replica answered."""


def _every_replica(client: Any, tree: PrivilegeTree, parameters: dict[str, Any]) -> _Rows:
    """The rows of every replica of `parameters["cluster"]`, where they all agree.

    Where they don't, it is the rows of the node `client` is connected to, as when only that
    node is read, with the replicas named: no row counts as present or absent from replicas
    that disagree.
    """
    replicas = [row["replica"] for row in _query_every_replica(client, "replicas", parameters)]
    if not replicas:
        raise _NoReplica("no replica answered.")
    copies = {
        name: _copies(_query_every_replica(client, f"replica_{name}", parameters), replicas)
        for name in _TABLES
    }
    if not all(table.agreed for table in copies.values()):
        names = {"names": parameters["names"]}
        disagreeing = tuple(sorted({r for table in copies.values() for r in table.held}))
        return replace(_node_rows(client, tree, names), agreed=False, disagreeing=disagreeing)
    return _Rows(
        tree,
        {kind: copies[f"{kind}s"].ordered() for kind in _KINDS},
        copies["grants"].ordered(),
        copies["role_grants"].ordered(),
        agreed=True,
    )


def _copies(rows: list[dict[str, Any]], replicas: list[str]) -> _Copies:
    """Each row by its text, and the texts each replica returned; none for a replica with
    no row."""
    by_text: dict[str, dict[str, Any]] = {}
    held: dict[str, set[str]] = {replica: set() for replica in replicas}
    for row in rows:
        replica = str(row.pop("replica"))
        text = json.dumps(row, sort_keys=True, default=str)
        by_text[text] = row
        held.setdefault(replica, set()).add(text)
    return _Copies(by_text, {replica: frozenset(texts) for replica, texts in held.items()})


def _node_rows(client: Any, tree: PrivilegeTree, names: dict[str, Any]) -> _Rows:
    """The rows of the node `client` is connected to."""
    tables = {name: _query(client, name, names) for name in _TABLES}
    return _Rows(
        tree,
        {kind: tables[f"{kind}s"] for kind in _KINDS},
        tables["grants"],
        tables["role_grants"],
    )


def _storage(client: Any) -> str | None:
    """The storage a create goes to; None when the login can't read `system.user_directories`,
    or it lists none that can take a create."""
    from clickhouse_connect.driver.exceptions import DatabaseError

    try:
        rows = _query(client, "storage")
    except DatabaseError as error:
        if "ACCESS_DENIED" in str(error):
            return None
        raise
    return str(rows[0]["type"]) if rows else None


def _accepted_sources(client: Any, sources: tuple[str, ...]) -> frozenset[str]:
    if not sources:
        return frozenset()
    rows = _query(client, "accepted_sources", {"sources": list(sources)})
    return frozenset(row["source"] for row in rows)


def _granted_to_all(client: Any, tree: PrivilegeTree) -> frozenset[str]:
    """CLUSTER, where the server grants it to every login; otherwise nothing.

    `CHECK GRANT` answers 1 for a right the server grants everyone, so CLUSTER is taken to
    be granted to all only where the admin's own grants, and its enabled roles', don't hold
    it. They are read through the server's privilege tree, so `ALL ON *.*` holds it too.
    """
    rights = rights_of(tree, _CLUSTER)
    if not rights or int(client.command(statement("live", "cluster_check"))) != 1:
        return frozenset()
    rows = _query(client, "own_grants")
    own = Holdings(tuple(stored for row in rows for stored in stored_rows(tree, row)))
    if all(own.holds(right) for right in rights):
        return frozenset()
    return frozenset(right.flag for right in rights)


def _query(client: Any, name: str, parameters: dict[str, Any] | None = None) -> list[dict]:
    result = client.query(statement("live", name), parameters=parameters or {})
    return list(result.named_results())


def _query_every_replica(client: Any, name: str, parameters: dict[str, Any]) -> list[dict]:
    """`_query`, where a replica that can't be reached fails the read (`_EVERY_REPLICA`)."""
    result = client.query(statement("live", name), parameters=parameters, settings=_EVERY_REPLICA)
    return list(result.named_results())


def _membership(row: dict[str, Any]) -> Membership:
    return Membership(
        row["granted_role_name"],
        default=bool(row["granted_role_is_default"]),
        admin_option=bool(row["with_admin_option"]),
    )


def _and(names: list[str]) -> str:
    """`a`, `a and b`, or `a, b and c`."""
    return " and ".join(names) if len(names) < 3 else ", ".join(names[:-1]) + f" and {names[-1]}"
