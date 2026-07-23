#!/usr/bin/env python3
"""Shared fail-closed SPDX license policy for release-bundled dependencies."""

from __future__ import annotations

from typing import Literal

from license_expression import ExpressionError, get_spdx_licensing

LicenseDisposition = Literal["approved", "conditional", "unresolved", "blocked"]

APPROVED_LICENSE_KEYS = frozenset({
    "0BSD",
    "Apache-2.0",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "BSL-1.0",
    "CC0-1.0",
    "CNRI-Python",
    "ISC",
    "LLVM-exception",
    "MIT",
    "MIT-0",
    "PSF-2.0",
    "Unicode-3.0",
    "Unlicense",
    "Zlib",
})
CONDITIONAL_LICENSE_KEYS = frozenset({
    "LGPL-2.1-or-later",
    "MPL-2.0",
})
_LICENSING = get_spdx_licensing()


def classify_release_license(expression: str) -> LicenseDisposition:
    """Classify an SPDX expression using an explicit release allowlist.

    Args:
        expression: SPDX license expression from exact dependency evidence.

    Returns:
        Approved, conditional, unresolved, or blocked. Unknown and malformed
        licenses are blocked rather than admitted by omission.
    """
    if expression == "NOASSERTION":
        return "unresolved"
    try:
        parsed = _LICENSING.parse(expression, validate=True, strict=True)
    except (ExpressionError, TypeError):
        return "blocked"
    if parsed is None:
        return "blocked"
    keys = set(_LICENSING.license_keys(parsed))
    allowed = APPROVED_LICENSE_KEYS | CONDITIONAL_LICENSE_KEYS
    if not keys or not keys <= allowed:
        return "blocked"
    if keys & CONDITIONAL_LICENSE_KEYS:
        return "conditional"
    return "approved"
