"""A password ch-migrate sets travels in the request body, never the URL.

A proxy's log, or a driver error that quotes the request, could show a URL. Every request the
driver sends during `bootstrap` and `access apply` is recorded at the HTTP pool.
"""

from urllib.parse import quote, quote_plus

import clickhouse_connect
import pytest
from clickhouse_connect.driver.httputil import get_pool_manager

pytestmark = pytest.mark.integration


def test_passwords_go_in_the_body(access_project, monkeypatch):
    project = access_project
    password = project.new_password() + "/+&"
    monkeypatch.setenv("CH_IT_ETL_PASSWORD", password)
    (project.root / "access.yaml").write_text(
        f"users:\n  {project.name}_etl:\n    password: {{secret: etl_password}}\n"
    )
    sent = _record_requests(monkeypatch)

    for args in (("bootstrap", "it"), ("access", "apply", "it", "--reset-passwords")):
        result = project.run(*args)
        assert result.exit_code == 0, (args, result.output)

    forms = {password, quote(password), quote(password, safe=""), quote_plus(password)}
    assert sent, "no request was recorded"
    assert not [url for url, _ in sent if any(form in url for form in forms)]
    bodies = [body if isinstance(body, bytes) else str(body).encode() for _, body in sent]
    assert any(form.encode() in body for body in bodies for form in forms)


def _record_requests(monkeypatch):
    """Every (url, body) the driver sends, through clients opened from now on."""
    sent = []
    get_client = clickhouse_connect.get_client

    def recording_client(**kwargs):
        pool = get_pool_manager()
        urlopen = pool.urlopen

        # style-ok: mirrors urllib3's PoolManager.urlopen, which it wraps
        def record(method, url, *args, **kw):
            sent.append((url, kw.get("body")))
            return urlopen(method, url, *args, **kw)

        pool.urlopen = record
        return get_client(pool_mgr=pool, **kwargs)

    monkeypatch.setattr(clickhouse_connect, "get_client", recording_client)
    return sent
