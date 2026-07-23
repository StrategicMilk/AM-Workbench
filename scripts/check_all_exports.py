#!/usr/bin/env python3
"""Validate that advertised ``__all__`` public exports resolve safely."""

from __future__ import annotations

import argparse
import gc
import importlib
import io
import os
import sys
import warnings
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts._all_exports_allowlist import (
    LEGACY_DEPRECATED_EXPORTS,
    LEGACY_PRIVATE_EXPORTS,
    PUBLIC_MODULES,
)


@dataclass(frozen=True)
class ExportFinding:
    """A public export contract violation."""

    module: str
    kind: str
    message: str
    symbol: str | None = None

    def format(self) -> str:
        target = f"{self.module}.{self.symbol}" if self.symbol else self.module
        return f"{target}: {self.kind}: {self.message}"


def _is_private_export(name: str) -> bool:
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


def _exports_for(module_name: str, module: ModuleType) -> tuple[list[str], list[ExportFinding]]:
    exports = getattr(module, "__all__", None)
    if exports is None:
        return [], []
    if isinstance(exports, str):
        return [], [ExportFinding(module_name, "invalid_all", "__all__ must not be a string")]
    try:
        export_list = list(exports)
    except TypeError:
        return [], [ExportFinding(module_name, "invalid_all", "__all__ must be iterable")]

    findings: list[ExportFinding] = []
    seen: set[str] = set()
    for name in export_list:
        if not isinstance(name, str):
            findings.append(ExportFinding(module_name, "invalid_symbol", "__all__ entries must be strings"))
            continue
        if name in seen:
            findings.append(ExportFinding(module_name, "duplicate_symbol", "duplicate __all__ entry", name))
        seen.add(name)
    return export_list, findings


def _allowed_deprecation(module_name: str, symbol: str, warning: warnings.WarningMessage) -> bool:
    if symbol not in LEGACY_DEPRECATED_EXPORTS.get(module_name, frozenset()):
        return False
    return issubclass(warning.category, DeprecationWarning)


def validate_module_exports(module_name: str, module: ModuleType) -> list[ExportFinding]:
    """Validate one imported module without mutating global warning filters."""

    exports, findings = _exports_for(module_name, module)
    allowed_private = LEGACY_PRIVATE_EXPORTS.get(module_name, frozenset())

    for name in exports:
        if not isinstance(name, str):
            continue
        if _is_private_export(name) and name not in allowed_private:
            findings.append(ExportFinding(module_name, "private_export", "private name is not allowlisted", name))

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            try:
                getattr(module, name)
            except AttributeError:
                findings.append(
                    ExportFinding(module_name, "missing_symbol", "symbol is listed in __all__ but absent", name)
                )
            except Exception as exc:
                findings.append(ExportFinding(module_name, "symbol_error", f"{type(exc).__name__}: {exc}", name))

        for warning in caught:
            if _allowed_deprecation(module_name, name, warning):
                continue
            findings.append(
                ExportFinding(
                    module_name,
                    "unexpected_warning",
                    f"{warning.category.__name__}: {warning.message}",
                    name,
                )
            )

    return findings


def collect_public_export_findings(module_names: Iterable[str] = PUBLIC_MODULES) -> list[ExportFinding]:
    """Import and validate all configured public export modules."""

    findings: list[ExportFinding] = []
    for module_name in module_names:
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:
            findings.append(ExportFinding(module_name, "import_error", f"{type(exc).__name__}: {exc}"))
            continue
        findings.extend(validate_module_exports(module_name, module))
    return findings


def _close_import_side_effect_devnull_handles() -> None:
    """Close leaked dev-null handles opened by imported export dependencies."""

    devnull_names = {os.devnull, os.path.basename(os.devnull)}
    for obj in gc.get_objects():
        if not isinstance(obj, io.IOBase) or obj.closed:
            continue
        if str(getattr(obj, "name", "")) not in devnull_names:
            continue
        obj.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "modules",
        nargs="*",
        default=list(PUBLIC_MODULES),
        help="Optional module names to validate instead of the repository public export allowlist.",
    )
    args = parser.parse_args(argv)

    findings = collect_public_export_findings(args.modules)
    _close_import_side_effect_devnull_handles()
    if findings:
        print(f"Public export check failed with {len(findings)} finding(s):")
        for finding in findings:
            print(f"- {finding.format()}")
        return 1

    print(f"Public export check passed for {len(args.modules)} module(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
