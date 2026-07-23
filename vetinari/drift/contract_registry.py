"""Contract Registry — vetinari.drift.contract_registry  (Phase 7).

Tracks a content-hash fingerprint for every versioned contract (dataclass,
schema dict, or plain dict).  When a fingerprint changes between runs the
registry raises ``ContractDriftError``, providing a precise diff for
engineers to review.

Usage
-----
    from vetinari.drift.contract_registry import get_contract_registry

    reg = get_contract_registry()
    reg.register("Plan", {"plan_id": "...", "goal": "...", "version": "v0.1.0"})
    reg.snapshot()          # persist current hashes to disk

    # Later — detect drift
    changed = reg.check_drift()
    for name, info in changed.items():
        logger.debug("%s: was %s, now %s", name, info['previous'], info['current'])
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from vetinari.constants import get_user_dir
from vetinari.learning.atomic_writers import write_json_atomic

logger = logging.getLogger(__name__)


_DEFAULT_SNAPSHOT_PATH: str | Path | None = None


def _resolve_default_snapshot_path() -> Path:
    """Resolve the default contract snapshot path, honoring test overrides."""
    if _DEFAULT_SNAPSHOT_PATH is not None:
        return Path(_DEFAULT_SNAPSHOT_PATH)
    return get_user_dir() / "drift_snapshots" / "contracts.json"


class ContractDriftError(Exception):
    """Raised when a registered contract fingerprint changes unexpectedly."""


def _fingerprint(obj: Any) -> str:
    """Return a stable SHA-256 hex digest of an object."""
    if is_dataclass(obj) and not isinstance(obj, type):
        data = asdict(obj)
    elif hasattr(obj, "to_dict"):
        data = obj.to_dict()
    elif isinstance(obj, dict):
        data = obj
    else:
        data = {"__repr__": repr(obj)}
    canonical = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


class ContractRegistry:
    """Thread-safe contract fingerprint registry.  Singleton — use.

    ``get_contract_registry()``.
    """

    _instance: ContractRegistry | None = None
    _class_lock = threading.Lock()

    def __new__(cls) -> ContractRegistry:
        if cls._instance is None:
            with cls._class_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._setup()
        return cls._instance

    def _setup(self) -> None:
        self._lock = threading.RLock()
        self._current: dict[str, str] = {}  # name → hash
        self._previous: dict[str, str] = {}  # name → hash (last snapshot)
        self._snapshot_path = _resolve_default_snapshot_path()
        self._snapshot_loaded: bool = False
        # True once any contract has been registered via register().  Used by
        # load_snapshot() to distinguish a cold-start load (bootstrap _current
        # from the snapshot) from a post-clear reload (only update _previous
        # so that drift detection sees cleared contracts as "REMOVED").
        self._ever_registered: bool = False

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, name: str, contract: Any) -> str:
        """Register and fingerprint a contract object.

        Args:
            name:     Unique contract name (e.g. ``"Plan"``, ``"AgentTask"``).
            contract: Dataclass instance, dict, or any object with ``to_dict()``.

        Returns:
            The SHA-256 hex digest of the contract.
        """
        h = _fingerprint(contract)
        with self._lock:
            self._current[name] = h
            self._ever_registered = True
        logger.debug("Registered contract '%s' → %s", name, h[:12])
        return h

    def register_many(self, contracts: dict[str, Any]) -> dict[str, str]:
        """Register multiple contracts at once.  Returns name → hash map."""
        return {name: self.register(name, obj) for name, obj in contracts.items()}

    # ------------------------------------------------------------------
    # Snapshot (persistence)
    # ------------------------------------------------------------------

    def snapshot(self, path: str | None = None, *, clock: Callable[[], float] | None = None) -> None:
        """Persist current fingerprints to disk."""
        p = Path(path) if path else self._snapshot_path
        p.parent.mkdir(parents=True, exist_ok=True)
        active_clock = clock or time.time
        with self._lock:
            payload = {
                "timestamp": active_clock(),
                "hashes": dict(self._current),
            }
        write_json_atomic(p, payload)
        logger.info("Contract snapshot written to %s (%d entries)", p, len(payload["hashes"]))

    def load_snapshot(self, path: str | None = None) -> bool:
        """Load a previously saved snapshot into ``_previous``.

        Returns:
            True if successful, False otherwise.

        """
        p = Path(path) if path else self._snapshot_path
        if not p.exists():
            with self._lock:
                self._previous.clear()
                self._snapshot_loaded = False
                if not self._ever_registered:
                    self._current.clear()
            logger.error("Contract snapshot missing: %s", p)
            return False
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
            hashes = payload.get("hashes", {})
            with self._lock:
                self._previous = dict(hashes)
                self._snapshot_loaded = True
                # Bootstrap _current from the snapshot only on a cold start —
                # when no contract has ever been registered on this instance.
                # On a post-clear reload (_ever_registered is True) we leave
                # _current empty so check_drift() correctly reports cleared
                # contracts as "REMOVED".
                if not self._ever_registered:
                    self._current = dict(hashes)
            logger.info("Loaded snapshot from %s (%d entries)", p, len(self._previous))
            return True
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            with self._lock:
                self._previous.clear()
                self._snapshot_loaded = False
                if not self._ever_registered:
                    self._current.clear()
            logger.error("Failed to load snapshot: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Drift detection
    # ------------------------------------------------------------------

    def check_drift(
        self,
        raise_on_drift: bool = False,
    ) -> dict[str, dict[str, str]]:
        """Compare current fingerprints against the loaded snapshot.

        Returns a dict of drifted contracts:
            ``{ name: {"previous": "...", "current": "..."} }``

        Args:
            raise_on_drift: If True, raises ``ContractDriftError`` when drift
                            is found.

        Returns:
            Mapping of contract name to a dict with ``"previous"`` and
            ``"current"`` SHA-256 digests for every contract whose fingerprint
            has changed, been added, or been removed since the last snapshot.
            Empty dict when no drift is detected.

        Raises:
            ContractDriftError: If ``raise_on_drift`` is True and any drift
                is found.
        """
        with self._lock:
            current = dict(self._current)
            previous = dict(self._previous)
            snapshot_loaded = self._snapshot_loaded

        if not snapshot_loaded:
            return {}

        drifted: dict[str, dict[str, str]] = {}

        # Changed or newly added contracts
        for name, h in current.items():
            prev_h = previous.get(name)
            if prev_h is None:
                drifted[name] = {"previous": "MISSING", "current": h}
                logger.warning("Contract drift: '%s' was added", name)
            elif prev_h != h:
                drifted[name] = {"previous": prev_h, "current": h}
                logger.warning("Contract drift: '%s' hash changed", name)

        # Removed contracts
        for name in previous:
            if name not in current:
                drifted[name] = {"previous": previous[name], "current": "REMOVED"}
                logger.warning("Contract drift: '%s' was removed", name)

        if drifted and raise_on_drift:
            names = ", ".join(drifted)
            raise ContractDriftError(f"Contract drift detected in: {names}")
        return drifted

    def is_stable(self) -> bool:
        """Return True when no drift is detected."""
        return len(self.check_drift()) == 0

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def list_contracts(self) -> list[str]:
        """List contracts.

        Returns:
            Alphabetically sorted list of all currently registered contract names.
        """
        with self._lock:
            return sorted(self._current.keys())

    def get_hash(self, name: str) -> str | None:
        """Get hash.

        Returns:
            The SHA-256 hex digest of the named contract's current fingerprint,
            or None if the contract has not been registered.
        """
        with self._lock:
            return self._current.get(name)

    def get_stats(self) -> dict[str, Any]:
        """Get stats.

        Returns:
            Dictionary with ``registered`` (number of contracts currently
            tracked), ``snapshotted`` (number loaded from the last on-disk
            snapshot), and ``drifted`` (number of contracts that differ from
            the snapshot).
        """
        with self._lock:
            return {
                "registered": len(self._current),
                "snapshotted": len(self._previous),
                "drifted": len(self.check_drift()),
            }

    def clear(self) -> None:
        """Clear for the current context."""
        with self._lock:
            self._current.clear()
            self._previous.clear()
            self._snapshot_loaded = False


# ---------------------------------------------------------------------------
# Singleton helpers
# ---------------------------------------------------------------------------


def get_contract_registry() -> ContractRegistry:
    """Return the global ContractRegistry singleton."""
    return ContractRegistry()


def reset_contract_registry() -> None:
    """Destroy the singleton (for tests)."""
    with ContractRegistry._class_lock:
        ContractRegistry._instance = None
