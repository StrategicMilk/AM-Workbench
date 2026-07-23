"""Credentials module — AES-256-GCM encrypted vault for API tokens and secrets.

Uses the hazmat AESGCM primitive (always available, including FIPS mode) rather than
Fernet (AES-CBC) which fails intermittently under Python 3.14 + cryptography 46.x
when other tests corrupt the OpenSSL provider state.

This is step 0 of the pipeline: credentials are loaded at startup before any agent
work begins.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from vetinari.constants import get_user_dir

logger = logging.getLogger(__name__)


try:
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    CRYPTO_AVAILABLE = True
except ImportError:
    CRYPTO_AVAILABLE = False
    logger.warning("cryptography library not available - credentials will not be encrypted")


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Atomically replace *path* with *data*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    try:
        tmp_path.write_bytes(data)
        tmp_path.replace(path)
    except Exception:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


def _atomic_write_json(path: Path, data: Any) -> None:
    """Atomically replace *path* with JSON data."""
    payload = json.dumps(data, indent=2).encode("utf-8")
    _atomic_write_bytes(path, payload)


@dataclass
class Credential:
    """Encrypted credential with metadata and rotation tracking."""

    source_type: str
    credential_type: str
    token: str
    scopes: list[str] = field(default_factory=list)
    rotation_days: int = 30
    last_rotated: str = ""
    next_rotation_due: str = ""
    access_controls: list[str] = field(default_factory=list)
    token_source: str = "manual"
    note: str = ""
    enabled: bool = True

    def __repr__(self) -> str:
        return (
            f"Credential(source_type={self.source_type!r}, "
            f"credential_type={self.credential_type!r}, "
            f"enabled={self.enabled!r})"
        )

    def to_dict(self) -> dict:
        """Serialize credential metadata to a plain dictionary, omitting the token.

        The token field is excluded to prevent accidental exposure in logs or API
        responses. Use the vault's ``get_token()`` to retrieve the token explicitly.

        Returns:
            Dictionary of all credential fields except ``token``.
        """
        d = asdict(self)
        d.pop("token", None)
        return d

    def needs_rotation(self) -> bool:
        """Check whether this credential has passed its scheduled rotation date.

        Returns:
            True if ``next_rotation_due`` is not set, unparseable, or in the past;
            False if the rotation due date is still in the future.
        """
        if not self.next_rotation_due:
            return True
        try:
            due = datetime.fromisoformat(self.next_rotation_due)
            if due.tzinfo is None:
                due = due.replace(tzinfo=timezone.utc)
            return datetime.now(timezone.utc) >= due
        except (ValueError, TypeError):
            # Unparseable date → treat as expired (forces rotation — safe default)
            logger.warning("Credential expiry date unparseable — treating as expired for safe rotation")
            expired = True
        return expired


class CredentialVault:
    """Credential vault."""

    def __init__(self, vault_path: str | None = None):
        if vault_path is None:
            vault_path = get_user_dir() / "vault"

        self.vault_path = Path(vault_path)
        self.vault_path.mkdir(parents=True, exist_ok=True)

        self.credentials_file = self.vault_path / "credentials.enc"
        self.meta_file = self.vault_path / "credentials_meta.json"

        self._legacy_fernet_key: bytes | None = None
        self._key = self._get_or_create_key()
        self._aesgcm = None
        if CRYPTO_AVAILABLE and self._key:
            try:
                self._aesgcm = AESGCM(self._key)
            except Exception as e:
                logger.warning("Failed to initialize AESGCM: %s", e)

        self._credentials: dict[str, Credential] = {}
        self._load()
        if self._legacy_fernet_key is not None and self._credentials:
            self._migrate_legacy_fernet_vault()

    def _get_or_create_key(self) -> bytes | None:
        """Load or generate the AES-256-GCM encryption key.

        Returns None when the cryptography library is not installed.
        Migrates legacy Fernet (base64-encoded) keys to raw 32-byte keys on
        first access — the old ciphertext is unreadable after migration, so
        the vault falls back to the plain-JSON path and re-encrypts on next save.

        Returns:
            32-byte raw key for AESGCM, or None if cryptography is unavailable.
        """
        if not CRYPTO_AVAILABLE:
            return None

        key_file = self.vault_path / ".key"
        if key_file.exists():
            try:
                raw = key_file.read_bytes()
                # Fernet keys are 44 bytes base64-encoded; AES-256 keys are 32 bytes raw.
                if len(raw) == 44:
                    logger.info("Detected legacy Fernet credential vault; deferring key migration until decrypt")
                    self._legacy_fernet_key = raw
                    return None
                if len(raw) != 32:
                    raise ValueError(f"Unsupported credential key length: {len(raw)} bytes")
                return raw
            except Exception:
                logger.warning("Failed to read encryption key file", exc_info=True)

        try:
            key = AESGCM.generate_key(bit_length=256)
            _atomic_write_bytes(key_file, key)
            try:
                key_file.chmod(0o600)
            except Exception:
                logger.warning("chmod on key file failed (expected on Windows)", exc_info=True)
            return key
        except Exception as e:
            logger.warning("Failed to generate encryption key: %s", e)
            return None

    def _load(self):
        if not self.credentials_file.exists():
            return

        try:
            encrypted = Path(self.credentials_file).read_bytes()

            if not encrypted:
                return

            # Try to decrypt if aesgcm is available
            if self._aesgcm:
                try:
                    nonce = encrypted[:12]
                    ciphertext = encrypted[12:]
                    decrypted = self._aesgcm.decrypt(nonce, ciphertext, None)
                    data = json.loads(decrypted)
                    for source_type, cred_data in data.items():
                        self._credentials[source_type] = Credential(**cred_data)
                    return
                except Exception as e:
                    logger.warning("Failed to decrypt credentials: %s", e)

            if self._legacy_fernet_key is not None:
                try:
                    decrypted = Fernet(self._legacy_fernet_key).decrypt(encrypted)
                    data = json.loads(decrypted)
                    for source_type, cred_data in data.items():
                        self._credentials[source_type] = Credential(**cred_data)
                    return
                except Exception:
                    logger.warning("Failed to decrypt legacy Fernet credential vault", exc_info=True)

            # Fail closed on plaintext credential stores.  Legacy encrypted
            # stores are migrated above; unencrypted JSON must be rotated.
            try:
                json.loads(encrypted.decode("utf-8"))
            except Exception:
                logger.warning("Credential store could not be decrypted or parsed as an encrypted vault")
            else:
                raise RuntimeError(
                    f"Refusing to load unencrypted credential store at {self.credentials_file}; "
                    "restore an encrypted vault or rotate the affected credentials."
                )
        except RuntimeError:
            raise
        except Exception as e:
            logger.warning("Could not load credentials: %s", e)

    def _migrate_legacy_fernet_vault(self) -> None:
        """Rewrite a decrypted legacy Fernet vault as AES-GCM with rollback."""
        if not CRYPTO_AVAILABLE:
            return

        key_file = self.vault_path / ".key"
        old_key = key_file.read_bytes() if key_file.exists() else b""
        old_credentials = self.credentials_file.read_bytes() if self.credentials_file.exists() else b""
        new_key = AESGCM.generate_key(bit_length=256)
        new_aesgcm = AESGCM(new_key)
        data = {k: asdict(v) for k, v in self._credentials.items()}
        nonce = os.urandom(12)
        new_credentials = nonce + new_aesgcm.encrypt(nonce, json.dumps(data).encode("utf-8"), None)

        try:
            _atomic_write_bytes(key_file, new_key)
            try:
                key_file.chmod(0o600)
            except Exception:
                logger.warning("chmod on key file failed (expected on Windows)", exc_info=True)
            _atomic_write_bytes(self.credentials_file, new_credentials)
            self._key = new_key
            self._aesgcm = new_aesgcm
            self._legacy_fernet_key = None
            self._save_meta()
            logger.info("Migrated legacy Fernet credential vault to AES-256-GCM")
        except Exception:
            logger.warning("Legacy credential vault migration failed; rolling back", exc_info=True)
            if old_key:
                _atomic_write_bytes(key_file, old_key)
            if old_credentials:
                _atomic_write_bytes(self.credentials_file, old_credentials)
            self._key = None
            self._aesgcm = None
            raise

    def _save(self):
        data = {k: asdict(v) for k, v in self._credentials.items()}
        json_str = json.dumps(data)

        # Encrypt if aesgcm is available (P1.H9: never silently fall back to plaintext)
        if self._aesgcm:
            # Let the exception propagate — a failed save is safer than a plaintext save.
            # Prepend the 96-bit nonce so _load() can extract it for decryption.
            nonce = os.urandom(12)  # 96-bit nonce for AES-GCM
            ciphertext = self._aesgcm.encrypt(nonce, json_str.encode(), None)
            _atomic_write_bytes(self.credentials_file, nonce + ciphertext)
        else:
            # P1.H9: Fail closed — refuse to store credentials without encryption.
            # This prevents accidental plaintext credential storage.
            raise RuntimeError(
                "Cannot save credentials: encryption is unavailable. "
                "Install the 'cryptography' package (`pip install cryptography`) "
                "to enable the credential vault.",
            )

        self._save_meta()

    def _save_meta(self):
        meta = {}
        for source_type, cred in self._credentials.items():
            meta[source_type] = cred.to_dict()

        _atomic_write_json(self.meta_file, meta)

    def get_credential(self, source_type: str) -> Credential | None:
        """Look up a stored credential by source type, returning only enabled ones.

        Args:
            source_type: The source type identifier (e.g. ``"github"``, ``"openai"``).

        Returns:
            The Credential if it exists and is enabled, or None if absent or disabled.
        """
        cred = self._credentials.get(source_type)
        if cred and cred.enabled:
            return cred
        return None

    def get_token(self, source_type: str) -> str | None:
        """Retrieve the raw token string for an enabled credential.

        Args:
            source_type: The source type identifier to look up.

        Returns:
            The token string if an enabled credential exists, or None.
        """
        cred = self.get_credential(source_type)
        return cred.token if cred else None

    def set_credential(self, source_type: str, credential: Credential) -> None:
        """Store a credential in the vault with rotation metadata.

        Args:
            source_type: The source type identifier (e.g. ``"github"``).
            credential: The credential to store.
        """
        credential.last_rotated = datetime.now(timezone.utc).isoformat()

        due = datetime.now(timezone.utc) + timedelta(days=credential.rotation_days)
        credential.next_rotation_due = due.isoformat()

        self._credentials[source_type] = credential
        self._save()
        logger.info("Credential set for %s", source_type)

    def remove_credential(self, source_type: str) -> None:
        """Remove a credential from the vault by source type.

        Args:
            source_type: The source type identifier to remove.
        """
        if source_type in self._credentials:
            del self._credentials[source_type]
            self._save()

    def rotate_credential(self, source_type: str, new_token: str) -> bool:
        """Replace the token for an existing credential and update its rotation timestamps.

        Args:
            source_type: The source type identifier of the credential to rotate.
            new_token: The replacement token value.

        Returns:
            True if the credential was found and successfully rotated, False if it
            does not exist in the vault.
        """
        if source_type not in self._credentials:
            return False

        cred = self._credentials[source_type]
        cred.token = new_token
        cred.last_rotated = datetime.now(timezone.utc).isoformat()

        due = datetime.now(timezone.utc) + timedelta(days=cred.rotation_days)
        cred.next_rotation_due = due.isoformat()

        self._save()
        logger.info("Credential rotated for %s", source_type)
        return True

    def list_credentials(self) -> dict[str, dict]:
        """Return all stored credentials as a dict of sanitised metadata.

        Returns:
            Mapping from source type to credential metadata (tokens excluded).
        """
        return {k: v.to_dict() for k, v in self._credentials.items()}

    def get_health(self) -> dict[str, Any]:
        """Return rotation health status for all stored credentials.

        Returns:
            Mapping from source type to a dictionary with keys: enabled, last_rotated,
            next_rotation_due, needs_rotation, credential_type, and note.
        """
        health = {}
        for source_type, cred in self._credentials.items():
            health[source_type] = {
                "enabled": cred.enabled,
                "last_rotated": cred.last_rotated,
                "next_rotation_due": cred.next_rotation_due,
                "needs_rotation": cred.needs_rotation(),
                "credential_type": cred.credential_type,
                "note": cred.note,
            }
        return health

    def is_admin(self, user_id: str) -> bool:
        """Check whether a user ID appears in the vault's administrators list.

        Args:
            user_id: The user identifier to check against the admins list.

        Returns:
            True if the user is listed in ``admins.json``, False if the file does not
            exist or the user is not listed.
        """
        admins_file = self.vault_path / "admins.json"
        if admins_file.exists():
            with Path(admins_file).open(encoding="utf-8") as f:
                admins = json.load(f)
                return user_id in admins.get("admins", [])
        return False

    def add_admin(self, user_id: str) -> None:
        """Add a user to the vault administrators list.

        Args:
            user_id: The user identifier to grant admin access.
        """
        admins_file = self.vault_path / "admins.json"
        admins = {"admins": []}
        if admins_file.exists():
            with Path(admins_file).open(encoding="utf-8") as f:
                admins = json.load(f)

        if user_id not in admins.get("admins", []):
            admins["admins"].append(user_id)
            _atomic_write_json(admins_file, admins)


class CredentialManager:
    """Credential manager."""

    def __init__(self):
        self.vault = CredentialVault()

    def get_token(self, source_type: str) -> str | None:
        """Retrieve the token string for a given source type.

        Args:
            source_type: The source type identifier to look up.

        Returns:
            The token string, or None if no enabled credential exists.
        """
        return self.vault.get_token(source_type)

    def has_credential(self, source_type: str) -> bool:
        """Check whether an enabled credential exists for the given source type.

        Args:
            source_type: The source type identifier to check.

        Returns:
            True if an enabled credential with a token exists, False otherwise.
        """
        return self.vault.get_token(source_type) is not None

    def set_credential(
        self,
        source_type: str,
        token: str,
        credential_type: str = "bearer",
        scopes: list[str] | None = None,
        rotation_days: int = 30,
        note: str = "",
    ) -> None:
        """Create and store a credential in the vault.

        Args:
            source_type: The source type identifier (e.g. ``"github"``).
            token: The raw token or secret value.
            credential_type: Token type such as ``"bearer"`` or ``"api_key"``.
            scopes: Optional list of permission scopes for this credential.
            rotation_days: Number of days before the credential should be rotated.
            note: Free-text note describing the credential purpose.
        """
        cred = Credential(
            source_type=source_type,
            credential_type=credential_type,
            token=token,
            scopes=scopes or [],
            rotation_days=rotation_days,
            note=note,
        )
        self.vault.set_credential(source_type, cred)

    def rotate(self, source_type: str, new_token: str) -> bool:
        """Rotate a credential by replacing its token value.

        Args:
            source_type: The source type identifier whose token to rotate.
            new_token: The replacement token value.

        Returns:
            True if the credential was found and rotated, False otherwise.
        """
        return self.vault.rotate_credential(source_type, new_token)

    def list(self) -> dict[str, dict]:
        """List all stored credentials as sanitised metadata.

        Returns:
            Mapping from source type to credential metadata (tokens excluded).
        """
        return self.vault.list_credentials()

    def health(self) -> dict[str, Any]:
        """Return health and rotation status for all stored credentials.

        Returns:
            Mapping from source type to health metadata including rotation info.
        """
        return self.vault.get_health()


_credential_manager: CredentialManager | None = None
_credential_lock = threading.Lock()


def get_credential_manager() -> CredentialManager:
    """Lazy-init singleton accessor for the credential manager.

    Uses double-checked locking to avoid module-level I/O on import.

    Returns:
        The shared CredentialManager instance.
    """
    global _credential_manager
    if _credential_manager is None:
        with _credential_lock:
            if _credential_manager is None:
                _credential_manager = CredentialManager()
    return _credential_manager
