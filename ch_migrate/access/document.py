"""Parse access.yaml as `yaml.safe_load` does, and find every key written twice in a mapping.

YAML keeps the last of two equal keys in one mapping and drops the first without a word, so
two `analyst:` entries would collapse into the second. Two keys are equal when YAML builds
equal values from them, as in the mapping `safe_load` returns: `1` and `1.0`, or `yes` and
`true`, are one key. A merge key (`<<`) repeats nothing, since merging is what it asks for.
"""

from __future__ import annotations

from collections.abc import Hashable
from dataclasses import dataclass, field
from typing import Any

import yaml
from yaml.constructor import SafeConstructor

from ch_migrate.access.reading import child

_MERGE_TAG = "tag:yaml.org,2002:merge"
_VALUE_TAG = "tag:yaml.org,2002:value"
_UNKEYED = object()  # a key the check leaves alone; building the document reports it


@dataclass(frozen=True)
class DuplicateKey:
    """A key written again in the same mapping: its key path, and where each one is."""

    path: str
    key: str
    first: yaml.Mark
    again: yaml.Mark

    @property
    def message(self) -> str:
        """`duplicate key analyst on lines 2 and 5; ...`, with 1-based lines."""
        if self.first.line == self.again.line:
            columns = f"columns {self.first.column + 1} and {self.again.column + 1}"
            where = f"line {self.first.line + 1}, {columns}"
        else:
            where = f"lines {self.first.line + 1} and {self.again.line + 1}"
        return f"duplicate key {self.key} on {where}; YAML would silently keep only the last"


@dataclass(frozen=True)
class Document:
    """One parsed YAML document, and every key it repeats, in the order they appear."""

    data: Any
    duplicates: tuple[DuplicateKey, ...] = ()


def parse_document(text: str) -> Document:
    """Parse one YAML document exactly as `yaml.safe_load` does, listing repeated keys.

    Raises `yaml.YAMLError` where `safe_load` would, with the same message.
    """
    loader = yaml.SafeLoader(text)
    try:
        node = loader.get_single_node()
        if node is None:
            return Document(None)
        duplicates = _Walk().run(node)
        return Document(loader.construct_document(node), duplicates)
    finally:
        loader.dispose()


@dataclass
class _Walk:
    """Visits every node once, aliases included, collecting repeated keys with their paths."""

    keys: SafeConstructor = field(default_factory=SafeConstructor)
    seen: set[int] = field(default_factory=set)
    found: list[DuplicateKey] = field(default_factory=list)

    def run(self, node: yaml.Node) -> tuple[DuplicateKey, ...]:
        self.visit(node, "")
        return tuple(self.found)

    def visit(self, node: yaml.Node, path: str) -> None:
        if id(node) in self.seen:
            return
        self.seen.add(id(node))
        if isinstance(node, yaml.SequenceNode):
            for index, item in enumerate(node.value):
                self.visit(item, f"{path}[{index}]")
        elif isinstance(node, yaml.MappingNode):
            self.mapping(node, path)

    def mapping(self, node: yaml.MappingNode, path: str) -> None:
        firsts: dict[Any, yaml.Node] = {}
        for key_node, value_node in node.value:
            key = self.key(key_node)
            key_text = _text(key_node, key)
            key_path = child(path, key_text)
            if key in firsts:
                first = firsts[key].start_mark
                again = key_node.start_mark
                self.found.append(DuplicateKey(key_path, key_text, first, again))
            elif key is not _UNKEYED:
                firsts[key] = key_node
            self.visit(value_node, key_path)

    def key(self, node: yaml.Node) -> Any:
        """The key as `safe_load` builds it, or `_UNKEYED` for a merge key or a bad key.

        A bad key, such as a list or an unknown tag, fails when the document is built.
        """
        if not isinstance(node, yaml.ScalarNode) or node.tag == _MERGE_TAG:
            return _UNKEYED
        if node.tag == _VALUE_TAG:
            return node.value  # `=`, which building the mapping reads as a string
        try:
            key = self.keys.construct_object(node, deep=True)
        except yaml.YAMLError:
            return _UNKEYED
        return key if isinstance(key, Hashable) else _UNKEYED


def _text(node: yaml.Node, key: Any) -> str:
    """How a key reads in a key path: as `str()` of the built key, like other problems."""
    if key is _UNKEYED:
        return str(node.value) if isinstance(node, yaml.ScalarNode) else "?"
    return str(key)
