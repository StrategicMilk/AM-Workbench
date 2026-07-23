"""Server-side user preferences persistence.

Replaces client-only localStorage with a JSON file store that syncs
to the browser on load and accepts updates via REST API.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from vetinari.constants import _PROJECT_ROOT
from vetinari.privacy import PRIVACY_ENVELOPE_KEY, privacy_receipt, require_privacy_envelope

logger = logging.getLogger(__name__)


_PREFS_PATH = _PROJECT_ROOT / ".vetinari" / "user_preferences.json"

# Whitelist of allowed preference keys (prevents arbitrary data injection)
ALLOWED_KEYS = frozenset({
    # Existing UI state
    "sidebarCollapsed",
    "reducedMotion",
    "compactMode",
    "theme",
    # Setup wizard
    "setupComplete",
    # Permissions
    "autonomyLevel",  # supervised | assisted | autonomous
    "allowModelDownload",  # ask | auto | deny
    "allowTrainingStart",  # ask | auto | deny
    "allowProjectExecute",  # ask | auto | deny
    "allowFileWrite",  # ask | auto | deny
    "allowDataCollection",  # ask | auto | deny
    # Notifications
    "notificationPreferences",
    "notificationDuration",  # int (ms), default 8000
    "notificationSound",  # bool
    # Appearance
    "interfaceMode",  # simple | standard | expert
    "accentColor",  # hex string
    "fontSize",  # int (px)
    "chatBubbleStyle",  # bubbles | flat | cards
    # Model paths
    "vetinari_image_models_dir",
    # User content
    "customInstructions",  # user-supplied global system prompt / instructions
    "defaultSystemPrompt",  # selected system prompt name
    # Task inference tuning (FSA-0051: parameter tuning round-trip)
    "taskInferenceParameters",  # per-task parameter overrides (temperature, max_tokens, top_p, etc.)
    "taskInferenceProfile",  # named profile selection (e.g. "creative", "precise")
    # Workbench operating posture
    "rigorLevel",  # selected Seriousness Dial level
    "rigorState",  # selected level plus project-scoped provenance receipt
    "trainingConfig",  # persisted training hyperparameters and required skill selection
})

# Default values matching current localStorage defaults
DEFAULTS: dict[str, Any] = {
    "sidebarCollapsed": False,
    "reducedMotion": False,
    "compactMode": False,
    "theme": "dark",
    "setupComplete": False,
    "autonomyLevel": "assisted",
    "notificationPreferences": "all",
    "allowModelDownload": "ask",
    "allowTrainingStart": "ask",
    "allowProjectExecute": "auto",
    "allowFileWrite": "ask",
    "allowDataCollection": "ask",
    "notificationDuration": 8000,
    "notificationSound": False,
    "interfaceMode": "standard",
    "accentColor": "#4e9af9",
    "fontSize": 14,
    "chatBubbleStyle": "flat",
    "vetinari_image_models_dir": "",
    "rigorLevel": "make_something",
    "rigorState": {},
    "trainingConfig": {},
}

_SUBJECT_DATA_KEYS = frozenset({"customInstructions", "defaultSystemPrompt", "users"})
MAX_USER_BUCKETS = 1024
MAX_PROFILES_PER_USER = 128


def _payload_requires_privacy_envelope(payload: dict[str, Any]) -> bool:
    return any(key in payload for key in _SUBJECT_DATA_KEYS)


def _preferences_privacy_receipt(payload: dict[str, Any]) -> dict[str, Any]:
    subject_data = _payload_requires_privacy_envelope(payload)
    return privacy_receipt(
        privacy_class="subject_data" if subject_data else "operational",
        subject_id="local-user" if subject_data else None,
        retention_days=365,
        source="web.preferences",
        redaction_applied=False,
    )


def _validate_user_capacity(user_id: str, *stores: dict[str, Any]) -> None:
    existing = set().union(*(store.keys() for store in stores))
    if user_id not in existing and len(existing) >= MAX_USER_BUCKETS:
        raise ValueError(f"preference user bucket limit exceeded ({MAX_USER_BUCKETS})")


def _validate_profile_capacity(name: str, profiles: dict[str, dict[str, Any]]) -> None:
    if name not in profiles and len(profiles) >= MAX_PROFILES_PER_USER:
        raise ValueError(f"preference profile limit exceeded ({MAX_PROFILES_PER_USER})")


class PreferencesManager:
    """Manages server-side user preferences with JSON file persistence.

    Damaged-state behavior is explicit: a missing preferences file uses
    defaults, a corrupt file is logged and ignored without overwrite, and a
    stale ``.tmp`` file from a prior crash is deleted before loading.
    """

    def __init__(self, path: Path | None = None):
        """Load preferences from disk, initialising from defaults when the file is absent.

        Args:
            path: Path to the JSON preferences file.  Defaults to
                ``_PREFS_PATH`` (``.vetinari/user_preferences.json``) when
                ``None``.
        """
        self._path = path or _PREFS_PATH
        self._prefs: dict[str, Any] = {}
        # FSA-0397 user-scoped preferences + named profiles:
        #   _user_prefs[user_id][key] = value
        #   _user_profiles[user_id][profile_name][key] = value
        # Both are loaded from / saved to the same JSON file under the
        # "users" key alongside the global _prefs block.
        self._user_prefs: dict[str, dict[str, Any]] = {}
        self._user_profiles: dict[str, dict[str, dict[str, Any]]] = {}
        self._load()

    def _load(self):
        """Load preferences from disk, falling back to defaults."""
        tmp_path = self._path.with_name(f".{self._path.name}.tmp")
        if tmp_path.exists():
            with contextlib.suppress(OSError):
                tmp_path.unlink()
        try:
            if self._path.exists():
                with Path(self._path).open(encoding="utf-8") as f:
                    saved = json.load(f)
                if not isinstance(saved, dict):
                    raise ValueError("preferences payload must be a JSON object")
                if _payload_requires_privacy_envelope(saved):
                    require_privacy_envelope(saved)
                # Only load allowed keys for the global bucket.
                self._prefs = {k: saved[k] for k in saved if k in ALLOWED_KEYS}
                # Load per-user buckets (FSA-0397).  Stored under "users";
                # each entry has {prefs: {...allowed only...}, profiles: {...}}.
                users_block = saved.get("users") if isinstance(saved, dict) else None
                if isinstance(users_block, dict):
                    for index, (user_id, bucket) in enumerate(users_block.items()):
                        if index >= MAX_USER_BUCKETS:
                            logger.warning(
                                "Ignoring preferences beyond user bucket limit %d in %s",
                                MAX_USER_BUCKETS,
                                self._path,
                            )
                            break
                        if not isinstance(user_id, str) or not isinstance(bucket, dict):
                            continue
                        prefs = bucket.get("prefs")
                        if isinstance(prefs, dict):
                            self._user_prefs[user_id] = {k: prefs[k] for k in prefs if k in ALLOWED_KEYS}
                        profiles = bucket.get("profiles")
                        if isinstance(profiles, dict):
                            self._user_profiles[user_id] = {
                                name: {k: profile[k] for k in profile if k in ALLOWED_KEYS}
                                for name, profile in list(profiles.items())[:MAX_PROFILES_PER_USER]
                                if isinstance(name, str) and isinstance(profile, dict)
                            }
                logger.debug(
                    "Loaded %d global preferences and %d user buckets from %s",
                    len(self._prefs),
                    len(self._user_prefs),
                    self._path,
                )
            else:
                self._prefs = {}
        except Exception as e:
            logger.warning("Failed to load preferences: %s", e)
            self._prefs = {}
            self._user_prefs = {}
            self._user_profiles = {}

    def _save(self):
        """Persist preferences to disk atomically.

        Raises:
            OSError: If the preference file cannot be written and replaced.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_name(f".{self._path.name}.tmp")
        # Serialise the global bucket alongside the per-user buckets
        # (FSA-0397).  Keep _prefs's keys at the top level so existing
        # operators / single-user installations can still read the file
        # without traversing the "users" object.
        payload: dict[str, Any] = dict(self._prefs)
        if self._user_prefs or self._user_profiles:
            users_block: dict[str, dict[str, Any]] = {}
            for user_id in set(self._user_prefs) | set(self._user_profiles):
                users_block[user_id] = {
                    "prefs": dict(self._user_prefs.get(user_id, {})),
                    "profiles": {name: dict(profile) for name, profile in self._user_profiles.get(user_id, {}).items()},
                }
            payload["users"] = users_block
        payload[PRIVACY_ENVELOPE_KEY] = _preferences_privacy_receipt(payload)
        try:
            with tmp_path.open("w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            tmp_path.replace(self._path)
        except Exception:
            logger.warning("Failed to save preferences to %s", self._path, exc_info=True)
            with contextlib.suppress(OSError):
                tmp_path.unlink()
            raise

    def get_all(self, *, user_id: str | None = None) -> dict[str, Any]:
        """Get all preferences with defaults applied.

        Args:
            user_id: Optional user scope.  When set, the returned dict
                layers the user's overrides on top of the global bucket
                (FSA-0397); when omitted, only the global bucket is
                returned (backward compatible).

        Returns:
            Dictionary mapping preference keys to their current values,
            with defaults filled in for any keys not explicitly set.
        """
        result = dict(DEFAULTS)
        result.update(self._prefs)
        if user_id is not None:
            result.update(self._user_prefs.get(user_id, {}))
        return result

    def get(self, key: str, *, user_id: str | None = None) -> Any:
        """Get a single preference value, optionally user-scoped (FSA-0397).

        Args:
            key: Preference key.
            user_id: Optional user scope.  When set, the user's value
                takes precedence over the global value (if any) and the
                schema default.

        Returns:
            The user-scoped value when present, else the global value,
            else the schema default, else ``None`` for unknown keys.
        """
        if user_id is not None:
            user_bucket = self._user_prefs.get(user_id)
            if user_bucket is not None and key in user_bucket:
                return user_bucket[key]
        return self._prefs.get(key, DEFAULTS.get(key))

    def set(self, key: str, value: Any, *, user_id: str | None = None) -> bool:
        """Set a single preference, persisting immediately to disk.

        Args:
            key: Preference key (must be in ALLOWED_KEYS).
            value: New value for the preference.
            user_id: Optional user scope.  When set, the write lands in
                that user's bucket rather than the global bucket
                (FSA-0397); when omitted, behavior matches the original
                single-user contract.

        Returns:
            True if the key is allowed and the value was saved,
            False if the key is not in the whitelist.

        Raises:
            OSError: if preference persistence fails.
        """
        if key not in ALLOWED_KEYS:
            return False
        if user_id is None:
            old_prefs = dict(self._prefs)
            self._prefs[key] = value
            try:
                self._save()
            except Exception:
                self._prefs = old_prefs
                raise
            return True
        # User-scoped write.
        _validate_user_capacity(user_id, self._user_prefs, self._user_profiles)
        old_user_prefs = {uid: dict(prefs) for uid, prefs in self._user_prefs.items()}
        self._user_prefs.setdefault(user_id, {})[key] = value
        try:
            self._save()
        except Exception:
            self._user_prefs = old_user_prefs
            raise
        return True

    # -----------------------------------------------------------------------
    # Profiles (FSA-0397)
    # -----------------------------------------------------------------------

    def create_profile(self, name: str, *, user_id: str, preferences: dict[str, Any]) -> dict[str, Any]:
        """Create or replace a named preference profile for *user_id*.

        Profiles are filtered against ``ALLOWED_KEYS`` on write — rejected
        keys are dropped silently so a profile creation cannot smuggle
        unknown keys past the whitelist.

        Args:
            name: Profile name (caller-chosen identifier).
            user_id: Owning user id.
            preferences: Key/value overrides this profile activates.

        Returns:
            The stored profile dict (post-allowlist filtering).

        Raises:
            OSError: if preference persistence fails.
        """
        filtered = {k: v for k, v in preferences.items() if k in ALLOWED_KEYS}
        _validate_user_capacity(user_id, self._user_prefs, self._user_profiles)
        _validate_profile_capacity(name, self._user_profiles.get(user_id, {}))
        old_profiles = {uid: {n: dict(p) for n, p in profs.items()} for uid, profs in self._user_profiles.items()}
        self._user_profiles.setdefault(user_id, {})[name] = filtered
        try:
            self._save()
        except Exception:
            self._user_profiles = old_profiles
            raise
        return dict(filtered)

    def list_profiles(self, *, user_id: str) -> dict[str, dict[str, Any]]:
        """Return every named profile for *user_id* (empty dict when none)."""
        return {name: dict(profile) for name, profile in self._user_profiles.get(user_id, {}).items()}

    def activate_profile(self, name: str, *, user_id: str) -> dict[str, Any]:
        """Activate *name* for *user_id*: merge its overrides into the user bucket.

        Args:
            name: Profile name (must have been created via ``create_profile``).
            user_id: Owning user id.

        Returns:
            The merged user-scoped preference dict after activation.

        Raises:
            KeyError: If *name* does not exist for *user_id*.
            OSError: if preference persistence fails.
        """
        profiles = self._user_profiles.get(user_id, {})
        if name not in profiles:
            raise KeyError(f"Profile {name!r} not found for user {user_id!r}")
        profile = profiles[name]
        old_user_prefs = {uid: dict(prefs) for uid, prefs in self._user_prefs.items()}
        bucket = self._user_prefs.setdefault(user_id, {})
        bucket.update(profile)
        try:
            self._save()
        except Exception:
            self._user_prefs = old_user_prefs
            raise
        return dict(bucket)

    def set_many(self, updates: dict[str, Any], *, user_id: str | None = None) -> dict[str, bool]:
        """Set multiple preferences at once, persisting all changes in a single write.

        Args:
            updates: Dictionary mapping preference keys to their new values.
            user_id: Optional user scope.  When set, writes land in that
                user's bucket rather than the global bucket (FSA-0397);
                when omitted, behavior matches the original single-user
                contract.

        Returns:
            Dictionary mapping each key to True if it was accepted (in ALLOWED_KEYS)
            or False if it was rejected.

        Raises:
            OSError: if preference persistence fails.
        """
        results: dict[str, bool] = {}
        changed = False
        if user_id is None:
            old_prefs = dict(self._prefs)
            for key, value in updates.items():
                if key in ALLOWED_KEYS:
                    self._prefs[key] = value
                    results[key] = True
                    changed = True
                else:
                    results[key] = False
            if changed:
                try:
                    self._save()
                except Exception:
                    self._prefs = old_prefs
                    raise
            return results
        # User-scoped writes (FSA-0397).
        _validate_user_capacity(user_id, self._user_prefs, self._user_profiles)
        old_user_prefs = {uid: dict(prefs) for uid, prefs in self._user_prefs.items()}
        bucket = self._user_prefs.setdefault(user_id, {})
        for key, value in updates.items():
            if key in ALLOWED_KEYS:
                bucket[key] = value
                results[key] = True
                changed = True
            else:
                results[key] = False
        if changed:
            try:
                self._save()
            except Exception:
                self._user_prefs = old_user_prefs
                raise
        return results

    def reset(self, key: str | None = None) -> None:
        """Reset one or all preferences to defaults.

        Raises:
            OSError: if preference persistence fails.
        """
        if key:
            old_prefs = dict(self._prefs)
            self._prefs.pop(key, None)
        else:
            old_prefs = dict(self._prefs)
            self._prefs.clear()
        try:
            self._save()
        except Exception:
            self._prefs = old_prefs
            raise


_manager: PreferencesManager | None = None
_manager_lock = threading.Lock()


def get_preferences_manager() -> PreferencesManager:
    """Get or create the global singleton preferences manager.

    Returns:
        The shared PreferencesManager instance, creating one on first call.
    """
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = PreferencesManager()
    return _manager


def reset_preferences_manager() -> None:
    """Reset the global preferences manager (for testing)."""
    global _manager
    _manager = None
