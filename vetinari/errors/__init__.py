"""Shared Vetinari error types."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from vetinari.errors.fail_closed import FailClosedError, require_mapping, require_present

_LEGACY_ERRORS_PATH = Path(__file__).resolve().parent.parent / "errors.py"
_LEGACY_SPEC = importlib.util.spec_from_file_location("_vetinari_legacy_errors", _LEGACY_ERRORS_PATH)
if _LEGACY_SPEC is None or _LEGACY_SPEC.loader is None:
    raise ImportError(f"could not load legacy error remediation module at {_LEGACY_ERRORS_PATH}")
_LEGACY_MODULE = importlib.util.module_from_spec(_LEGACY_SPEC)
sys.modules[_LEGACY_SPEC.name] = _LEGACY_MODULE
_LEGACY_SPEC.loader.exec_module(_LEGACY_MODULE)

ErrorRemediation = _LEGACY_MODULE.ErrorRemediation
ERROR_REMEDIATIONS = _LEGACY_MODULE.ERROR_REMEDIATIONS
find_remediation = _LEGACY_MODULE.find_remediation
format_remediation = _LEGACY_MODULE.format_remediation

__all__ = [
    "ERROR_REMEDIATIONS",
    "ErrorRemediation",
    "FailClosedError",
    "find_remediation",
    "format_remediation",
    "require_mapping",
    "require_present",
]
