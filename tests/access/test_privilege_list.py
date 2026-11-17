"""The bundled privilege list (Decision 11), from ClickHouse 26.3's `system.privileges`.

`tests/integration/test_access_privilege_list.py` pins it to the image; these check what
`validate` relies on.
"""

import json

from ch_migrate.access.privileges import BUNDLED_LIST, bundled_tree

DOCUMENT = json.loads(BUNDLED_LIST.read_text())
TREE = bundled_tree()


def test_the_list_comes_from_26_3():
    assert DOCUMENT["version"].startswith("26.3.")
    assert len(DOCUMENT["privileges"]) > 200
    row = DOCUMENT["privileges"][0]
    assert set(row) == {"privilege", "aliases", "level", "parent_group"}


def test_aliases_resolve_to_canonical_names():
    assert TREE.canonical("delete") == "ALTER DELETE"
    assert TREE.canonical("ALTER  DELETE") == "ALTER DELETE"
    assert TREE.canonical("SOURCE READ") == "READ"
    assert TREE.canonical("NOPE") is None


def test_read_and_write_are_source_level():
    assert (TREE.levels["READ"], TREE.levels["WRITE"]) == ("SOURCE", "SOURCE")
    assert TREE.flags("READ", "source") == {"READ"}
    assert TREE.flags("READ", "database") == frozenset()


def test_old_source_spellings_name_their_source():
    spellings = DOCUMENT["source_spellings"]
    assert len(spellings) == 18
    assert (spellings["REMOTE"], spellings["S3"], spellings["SOURCES"]) == ("REMOTE", "S3", "")
    assert TREE.new_source_spelling("REMOTE") == "REMOTE"
    assert TREE.new_source_spelling("SOURCES") == "*.*"
    assert TREE.new_source_spelling("CLUSTER") is None
    assert TREE.source("remote") == "REMOTE"


def test_a_group_means_its_childrens_flags_where_the_scope_allows():
    create = TREE.flags("CREATE", "database")
    assert {"CREATE DATABASE", "CREATE TABLE", "CREATE VIEW", "CREATE DICTIONARY"} <= create
    assert "CREATE DATABASE" not in TREE.flags("ALTER", "table")
    assert "ALTER DATABASE" not in TREE.flags("ALTER", "table")
    assert TREE.flags("CLUSTER", "database") == frozenset()
