"""Deprecated alias for `ch_migrate`, the import name before 0.5. Removed in 1.0.

`from clickhouse_alembic import run_sql` and `from clickhouse_alembic.config import ...`
(as in env.py files generated before 0.5) keep working. Every `clickhouse_alembic.X`
is the same module object as `ch_migrate.X`, so classes and state are shared rather
than loaded twice.
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.util
import sys
import warnings
from types import FrameType, ModuleType
from typing import Any, Optional

_NEW = "ch_migrate"
_MESSAGE = (
    "clickhouse_alembic was renamed to ch_migrate in 0.5; update this import. "
    "The old name stops working in 1.0."
)


def __getattr__(name: str) -> Any:
    return getattr(importlib.import_module(_NEW), name)


class _AliasFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Resolve `clickhouse_alembic.X` to the already-importable `ch_migrate.X`."""

    def find_spec(self, fullname: str, path: Any, target: Any = None) -> Any:
        if not fullname.startswith(__name__ + "."):
            return None
        return importlib.util.spec_from_loader(fullname, self)

    def create_module(self, spec: Any) -> ModuleType:
        return importlib.import_module(_NEW + spec.name[len(__name__) :])

    def exec_module(self, module: ModuleType) -> None:
        pass  # create_module returned the real, already-executed module


def _warn_at_importer() -> None:
    """Point the warning at the file that wrote the old import, not at importlib."""
    frame: Optional[FrameType] = sys._getframe(1)
    while frame is not None and (
        "importlib" in frame.f_code.co_filename or frame.f_code.co_filename == __file__
    ):
        frame = frame.f_back
    if frame is None:
        warnings.warn(_MESSAGE, FutureWarning, stacklevel=2)
    else:
        warnings.warn_explicit(_MESSAGE, FutureWarning, frame.f_code.co_filename, frame.f_lineno)


sys.meta_path.insert(0, _AliasFinder())
_warn_at_importer()
