"""Does a URL ?session_timeout= reach the server through the official dialect?"""
import sys
import time

from sqlalchemy import create_engine, pool, text

url = "clickhousedb://default:spike@127.0.0.1:18123/default" + sys.argv[1]
with create_engine(url, poolclass=pool.NullPool).connect() as conn:
    conn.execute(text("SET max_threads = 1"))
    before = conn.execute(text("SELECT getSetting('max_threads')")).scalar()
    time.sleep(4)
    after = conn.execute(text("SELECT getSetting('max_threads')")).scalar()
    print(f"url suffix {sys.argv[1]!r:24s} before idle={before} after 4s idle={after}")
