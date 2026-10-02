"""Q5 battery: run tricky SQL through Alembic op.execute on either dialect.

usage: python q5_battery.py official|legacy
"""
import sys

from alembic.ddl import impl
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, pool, text

FLAVOR = sys.argv[1]
if FLAVOR == "official":
    import clickhouse_connect.cc_sqlalchemy.alembic  # noqa: F401
    URL = "clickhousedb://default:spike@127.0.0.1:18125/spike_oc"
else:
    class ClickhouseImpl(impl.DefaultImpl):  # same as clickhouse_alembic/env.py
        __dialect__ = "clickhouse"
        transactional_ddl = False
    URL = "clickhouse+http://default:spike@127.0.0.1:18125/spike_oc"

CASES = [
    ("multi-statement", "SELECT 1; SELECT 2"),
    ("trailing ;", "SELECT 1;"),
    ("colon after quote", "SELECT ':abc'"),
    ("colon in time literal", "SELECT toDateTime('2024-01-01 12:30:00')"),
    ("colon in /* comment */", "SELECT 1 /* see :ref */"),
    ("backslash-escaped colon", "SELECT '\\:abc'"),
    ("pg-style :: cast", "SELECT 1::String"),
    ("percent / LIKE", "SELECT 'a%b' LIKE 'a%'"),
    ("CH param syntax in literal", "SELECT '{x:String}'"),
    ("JSON literal", "SELECT '{\"a\":1}'"),
    ("ON CLUSTER DDL", "CREATE TABLE IF NOT EXISTS spike_oc.t_oc_" + FLAVOR
     + " ON CLUSTER spike_cluster (a UInt8) ENGINE = MergeTree ORDER BY a"),
    ("ON CLUSTER bad DDL", "ALTER TABLE spike_oc.no_such_table ON CLUSTER spike_cluster ADD COLUMN b UInt8"),
]


def main():
    engine = create_engine(URL, poolclass=pool.NullPool)
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn)
        op = Operations(ctx)
        print(f"== {FLAVOR}: impl={type(ctx.impl).__name__} __dialect__={ctx.impl.__dialect__!r}")
        for label, sql in CASES:
            try:
                op.execute(sql)
                rows = conn.execute(text("SELECT 'op.execute ok'")).scalar()
                print(f"  OK    {label:28s} {sql!r}")
            except Exception as e:  # noqa: BLE001 - report every failure mode
                msg = str(e).splitlines()[0][:150]
                print(f"  FAIL  {label:28s} {sql!r}\n        -> {type(e).__name__}: {msg}")
        if FLAVOR == "official":
            res = conn.exec_driver_sql(
                "CREATE TABLE IF NOT EXISTS spike_oc.t_oc_rows ON CLUSTER spike_cluster"
                " (a UInt8) ENGINE = MergeTree ORDER BY a")
            print("  ON CLUSTER result rows via exec_driver_sql:", res.fetchall())


main()
