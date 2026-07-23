"""Native cloud SDK client factories.

The factories are intentionally small and lazy: importing this package must not
import optional cloud SDKs or create network clients. Each provider module owns a
lock-protected singleton for the SDK client it wraps.
"""

from __future__ import annotations

__all__: list[str] = []
