"""exec_driver_sql (no text() bind parsing): what reaches the server?"""
import sys

from sqlalchemy import create_engine, pool

URL = {
    "official": "clickhousedb://default:spike@127.0.0.1:18125/spike_oc",
    "legacy": "clickhouse+http://default:spike@127.0.0.1:18125/spike_oc",
}[sys.argv[1]]
CASES = [
    "SELECT '{\"a\":1}'",
    "SELECT ':abc'",
    "SELECT 'a%b'",
    "SELECT 'a%%b'",
    "SELECT '%(x)s'",
]
with create_engine(URL, poolclass=pool.NullPool).connect() as conn:
    for sql in CASES:
        try:
            print(f"  {sys.argv[1]:8s} sent {sql!r:22s} -> got {conn.exec_driver_sql(sql).scalar()!r}")
        except Exception as e:  # noqa: BLE001
            print(f"  {sys.argv[1]:8s} sent {sql!r:22s} -> {type(e).__name__}: {str(e).splitlines()[0][:110]}")
