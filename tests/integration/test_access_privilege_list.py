"""Pin the bundled privilege list to the ClickHouse 26.3 image it came from (Decision 11).

`access validate` checks names offline against `ch_migrate/access/privileges_26_3.json`.
This records the list again from the test server, the way `scripts/bundle_privileges.py`
does, and fails when the two differ. When the floating `26.3` tag changes the list, run the
script and review the diff. Other server versions skip: the list is 26.3's by design.
"""

import json
import secrets

import pytest

from ch_migrate.access.bundle import record_privileges
from ch_migrate.access.privileges import BUNDLED_LIST

pytestmark = pytest.mark.integration


def test_the_bundled_list_matches_the_26_3_image(clickhouse_server):
    client = clickhouse_server.connect()
    try:
        if not client.server_version.startswith("26.3."):
            pytest.skip(f"the bundled list is 26.3's; this server is {client.server_version}")
        recorded = record_privileges(client, "it_" + secrets.token_hex(8) + "_scratch")
    finally:
        client.close()
    bundled = json.loads(BUNDLED_LIST.read_text())
    assert recorded["privileges"] == bundled["privileges"]
    assert recorded["source_spellings"] == bundled["source_spellings"]
