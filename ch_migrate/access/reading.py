"""Reading access YAML: key paths, placeholders, names, secrets and grant entries.

A `Reader` collects problems instead of raising, so `access validate` can list every error
at once. Each problem names the file, the key path and the reason. A value that fails a
check is left out, and reading carries on with the rest.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import yaml

from ch_migrate.access.grants import GrantSyntaxError, check_grant, parse_grant
from ch_migrate.access.model import Grant, Secret
from ch_migrate.access.privileges import PrivilegeTree
from ch_migrate.bootstrap import validate_identifier
from ch_migrate.sql import render_placeholders

_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_GRANT_KEYS = ("grant", "best_effort")


@dataclass(frozen=True)
class Problem:
    """An error or warning: the file, the key path in it, and the reason."""

    file: str
    path: str
    message: str

    def __str__(self) -> str:
        return ": ".join(part for part in (self.file, self.path, self.message) if part)


@dataclass
class Reader:
    """Reads values from one YAML file, rendering placeholders and collecting problems.

    `canonical` rewrites privilege aliases to canonical names, as `access.yaml` needs. The
    built-in files keep 0.8's spelling, so legacy mode renders 0.8's statements exactly.
    """

    file: str
    values: dict[str, str]
    tree: PrivilegeTree
    canonical: bool = True
    errors: list[Problem] = field(default_factory=list)
    warnings: list[Problem] = field(default_factory=list)

    def mapping(self, value: Any, path: str, keys: tuple[str, ...]) -> dict[str, Any] | None:
        """`value` as a mapping holding only `keys`; null reads as empty. None if not a mapping."""
        if value is None:
            return {}
        if not isinstance(value, dict):
            self.error(path, f"expected a mapping, found {describe(value)}")
            return None
        fields = {}
        for key, item in value.items():
            if key in keys:
                fields[key] = item
            else:
                self.error(child(path, str(key)), f"unknown key; the keys here are {_or(keys)}")
        return fields

    def grants(self, value: Any, path: str) -> tuple[Grant, ...]:
        """A list of grant entries: strings, or `{grant: ..., best_effort: true}`."""
        if not isinstance(value, list):
            self.error(path, f"expected a list of grants, found {describe(value)}")
            return ()
        grants = (self._grant(entry, f"{path}[{index}]") for index, entry in enumerate(value))
        return tuple(grant for grant in grants if grant is not None)

    def names(self, value: Any, path: str) -> tuple[str, ...]:
        """A list of role names."""
        if not isinstance(value, list):
            self.error(path, f"expected a list of role names, found {describe(value)}")
            return ()
        names = (self.name(item, f"{path}[{index}]") for index, item in enumerate(value))
        return tuple(name for name in names if name is not None)

    def flag(self, value: Any, path: str) -> bool:
        if not isinstance(value, bool):
            self.error(path, f"expected true or false, found {describe(value)}")
            return False
        return value

    def secret(self, value: Any, path: str) -> Secret | None:
        """`{secret: NAME}`: never a password itself."""
        if not isinstance(value, dict) or set(value) != {"secret"}:
            self.error(path, "expected {secret: NAME}; access.yaml never holds a password")
            return None
        name = self.name(value["secret"], child(path, "secret"))
        return None if name is None else Secret(name)

    def name(self, value: Any, path: str) -> str | None:
        """A user or role name, rendered, that `validate_identifier` accepts."""
        if isinstance(value, dict):
            self.error(path, "found a mapping: quote a name that starts with {, as in '{db}_x'")
            return None
        if not isinstance(value, str):
            self.error(path, f"expected a name, found {describe(value)}")
            return None
        rendered = self.render(value, path)
        try:
            return None if rendered is None else validate_identifier(rendered, "name")
        except ValueError as error:
            self.error(path, str(error))
            return None

    def render(self, text: str, path: str) -> str | None:
        """Fill in the placeholders; an unknown one is an error."""
        rendered = render_placeholders(text, self.values)
        unknown = _PLACEHOLDER.findall(rendered)
        if unknown:
            known = _or(tuple("{" + name + "}" for name in self.values))
            self.error(path, f"unknown placeholder {{{unknown[0]}}}; the placeholders are {known}")
            return None
        return rendered

    def error(self, path: str, message: str) -> None:
        self.errors.append(Problem(self.file, path, message))

    def warn(self, path: str, message: str) -> None:
        self.warnings.append(Problem(self.file, path, message))

    def _grant(self, entry: Any, path: str) -> Grant | None:
        text, best_effort = self._grant_entry(entry, path)
        if text is None:
            return None
        rendered = self.render(text, path)
        if rendered is None:
            return None
        try:
            grant = parse_grant(rendered, best_effort=best_effort)
        except GrantSyntaxError as error:
            self.error(path, f"`{text}`: {error}")
            return None
        checked = check_grant(grant, self.tree)
        for message in checked.errors:
            self.error(path, f"`{text}`: {message}")
        for message in checked.warnings:
            self.warn(path, f"`{text}`: {message}")
        if checked.errors:
            return None
        return checked.grant if self.canonical else grant

    def _grant_entry(self, entry: Any, path: str) -> tuple[str | None, bool]:
        """The grant text and whether it is best-effort."""
        if isinstance(entry, str):
            return entry, False
        if not isinstance(entry, dict):
            found = describe(entry)
            self.error(
                path, f"expected a grant or {{grant: ..., best_effort: true}}, found {found}"
            )
            return None, False
        fields = self.mapping(entry, path, _GRANT_KEYS) or {}
        best_effort = self.flag(fields.get("best_effort", False), child(path, "best_effort"))
        if not isinstance(fields.get("grant"), str):
            self.error(child(path, "grant"), "expected the grant as a string")
            return None, best_effort
        return fields["grant"], best_effort


def child(path: str, key: str) -> str:
    """The key path of `key` inside `path`."""
    return f"{path}.{key}" if path else key


def describe(value: Any) -> str:
    """What a YAML value is, in words: `a list`, `a string`, `nothing`."""
    if value is None:
        return "nothing"
    if isinstance(value, bool):
        return "true or false"
    kinds = {dict: "a mapping", list: "a list", str: "a string", int: "a number"}
    return next((word for kind, word in kinds.items() if isinstance(value, kind)), "a value")


def yaml_problem(error: yaml.YAMLError) -> tuple[str, str]:
    """Where a YAML error is (`line 3, column 5`, or empty) and what it is."""
    mark = getattr(error, "problem_mark", None)
    where = f"line {mark.line + 1}, column {mark.column + 1}" if mark else ""
    return where, f"not valid YAML: {getattr(error, 'problem', None) or error}"


def _or(choices: tuple[str, ...]) -> str:
    return ", ".join(choices[:-1]) + f" or {choices[-1]}" if len(choices) > 1 else choices[0]
