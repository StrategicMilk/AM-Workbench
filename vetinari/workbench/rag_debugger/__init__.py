"""Package entrypoint for the RAG debugger workbench surface.

The original implementation lives in the sibling ``rag_debugger.py`` module.
This package wrapper lets callers import both ``vetinari.workbench.rag_debugger``
and its submodules such as ``vetinari.workbench.rag_debugger.experiments_store``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_IMPL_NAME = "vetinari.workbench._rag_debugger_impl"
_IMPL_PATH = Path(__file__).resolve().with_name("__init__.py").parent.with_suffix(".py")

_impl = sys.modules.get(_IMPL_NAME)
if _impl is None:
    _spec = importlib.util.spec_from_file_location(_IMPL_NAME, _IMPL_PATH)
    if _spec is None or _spec.loader is None:
        raise ImportError(f"cannot load RAG debugger implementation from {_IMPL_PATH}")
    _impl = importlib.util.module_from_spec(_spec)
    sys.modules[_IMPL_NAME] = _impl
    _spec.loader.exec_module(_impl)

__all__ = list(getattr(_impl, "__all__", ()))

for _name in __all__:
    globals()[_name] = getattr(_impl, _name)
