"""Distributed DDL receipts identified by an owned queue marker, never query text."""

from __future__ import annotations

from ch_migrate.waiting_types import UnknownOutcome, WaitingError


class DistributedDDLWaiter:
    def __init__(self, client, budget):
        self.client, self.budget = client, budget

    def prepare(self, cluster: str, token: str) -> dict:
        hosts = self.client.query(
            "SELECT host_name, port FROM system.clusters WHERE cluster = {cluster:String} "
            "ORDER BY shard_num, replica_num",
            parameters={"cluster": cluster},
        ).result_rows
        if not hosts:
            raise WaitingError(f"Cluster {cluster!r} has no configured DDL hosts")
        return {
            "cluster": cluster,
            "token": token,
            "hosts": [list(host) for host in hosts],
            "entry": None,
            "done_hosts": [],
        }

    def wait(self, receipt: dict, checkpoint) -> None:
        expected = {_host_key(host, port) for host, port in receipt["hosts"]}
        while not expected.issubset(receipt["done_hosts"]):
            rows = self._rows(receipt)
            entries = {row[0] for row in rows}
            if not entries:
                raise UnknownOutcome(
                    "Distributed DDL outcome unknown: queue evidence is missing or expired; statement not reissued"
                )
            if len(entries) != 1:
                raise UnknownOutcome(
                    "Distributed DDL ownership marker identifies multiple queue entries; reconcile without resubmitting"
                )
            entry = next(iter(entries))
            if receipt["entry"] is not None and receipt["entry"] != entry:
                raise UnknownOutcome(
                    "Distributed DDL queue entry changed for a recorded intent; statement not reissued"
                )
            if receipt["entry"] is None:
                receipt["entry"] = entry
                checkpoint()
            pending = self._host_states(receipt, rows, checkpoint)
            if pending:
                self.budget.pause(f"DDL {entry}: " + "; ".join(pending))
        self.budget.complete(f"DDL {receipt['entry']}; every required host finished")

    def tables(
        self, table: tuple[str, str], cluster: str, allow_missing: bool = False
    ) -> dict | None:
        rows = self.client.query(
            "SELECT materialize(hostName()), toString(uuid), engine, sorting_key FROM "
            "clusterAllReplicas({cluster:String}, system.tables) "
            "WHERE database = {db:String} AND name = {table:String}",
            parameters={"cluster": cluster, "db": table[0], "table": table[1]},
            settings={
                "skip_unavailable_shards": int(allow_missing),
                "connect_timeout_with_failover_ms": 250,
            },
        ).result_rows
        if not rows:
            return None
        if len({row[2] for row in rows}) != 1:
            raise WaitingError(
                "Distributed DDL target engines differ across hosts; reconcile them before tracked execution"
            )
        return {
            "database": table[0],
            "table": table[1],
            "cluster": cluster,
            "ddl_scope": True,
            "engine": rows[0][2],
            "sorting_key": rows[0][3],
            "initiator": rows[0][0],
            "uuids": {host: uuid for host, uuid, _, _ in rows},
        }

    def _rows(self, receipt: dict) -> list:
        return self.client.query(
            "SELECT entry, host, port, status, exception_code, exception_text "
            "FROM system.distributed_ddl_queue WHERE settings['log_comment'] = {token:String}",
            parameters={"token": receipt["token"]},
        ).result_rows

    def _host_states(self, receipt: dict, rows: list, checkpoint) -> list[str]:
        observed = {}
        for _, host, port, status, code, message in rows:
            key = _host_key(host, port)
            if code:
                raise WaitingError(
                    f"Distributed DDL {receipt['entry']} failed on {key}: code {code}: {message}"
                )
            observed[key] = status
            if status == "Finished" and code == 0 and key not in receipt["done_hosts"]:
                receipt["done_hosts"].append(key)
                checkpoint()
        pending = []
        for host, port in receipt["hosts"]:
            key = _host_key(host, port)
            if key not in receipt["done_hosts"]:
                pending.append(f"host {key}: {observed.get(key) or 'missing queue row'}")
        return pending


def _host_key(host, port) -> str:
    return f"{host}:{port}"
