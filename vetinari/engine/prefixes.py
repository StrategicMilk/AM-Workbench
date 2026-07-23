"""Source-guarded registration for PromptAssembler static prefixes."""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from vetinari.engine.client_types import PrefixRef

if TYPE_CHECKING:
    from vetinari.prompts.assembler import PromptAssembler


class PrefixNameCollisionError(RuntimeError):
    """A stable prefix name was reused for different content."""


_STATE_DIR = Path(__file__).parent / "state"
_STATE_FILE = _STATE_DIR / "static-prefixes.json"
_STATE_LOCK = threading.Lock()


def _read_registry() -> dict[str, str]:
    if not _STATE_FILE.exists():
        return {}
    payload = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in payload.items()
    ):
        raise ValueError("static prefix registry must be a string-to-string object")
    return payload


def _write_registry(registry: dict[str, str]) -> None:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = _STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(registry, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(_STATE_FILE)


def _source_blocks(assembler: PromptAssembler) -> tuple[tuple[str, str], ...]:
    """Build only PromptAssembler-owned content; callers cannot inject strings."""
    static_prefix = assembler._build_static_prefix("general", "general", None, 4000, None, None)
    rules_prefix = assembler._load_rules_prefix(None, None)
    blocks = (("prompt-assembler-static", static_prefix), ("prompt-assembler-rules", rules_prefix))
    return tuple((name, content) for name, content in blocks if content)


def register_static_blocks(assembler: PromptAssembler) -> list[PrefixRef]:
    """Register PromptAssembler-owned static blocks idempotently by SHA-256.

    Returns:
        Ordered exact prefix references for all non-empty source-owned blocks.

    Raises:
        PrefixNameCollisionError: If a stable name resolves to new content.
        ValueError: If persisted state or the engine response is malformed.
    """
    from vetinari.engine import get_engine_client

    refs: list[PrefixRef] = []
    with _STATE_LOCK:
        registry = _read_registry()
        changed = False
        for name, content in _source_blocks(assembler):
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            existing = registry.get(name)
            if existing is not None and existing != digest:
                raise PrefixNameCollisionError(f"prefix {name!r} changed from {existing} to {digest}")
            if existing == digest:
                refs.append(PrefixRef(prefix_name=name, content_hash=digest))
                continue
            ref = get_engine_client().register_prefix(name, content, content_hash=digest)
            if ref.content_hash != digest or ref.prefix_name != name:
                raise ValueError("AM Engine returned a prefix reference that does not match registered content")
            registry[name] = digest
            refs.append(ref)
            changed = True
        if changed:
            _write_registry(registry)
    return refs


__all__ = ["PrefixNameCollisionError", "register_static_blocks"]
