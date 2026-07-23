"""Administrative model, adapter, prefix, and session operations for the engine client."""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Mapping
from math import isfinite
from pathlib import PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Any

from vetinari.engine.client_types import EngineProtocolError, PrefixRef
from vetinari.exceptions import EngineUnavailableError


class _EngineAdminMixin:
    """Administrative operations composed into the shared engine transport."""

    if TYPE_CHECKING:
        _prefix_counts: dict[tuple[str, str], int]
        _prefix_lock: threading.RLock

        def _admin(
            self,
            method: str,
            path: str,
            payload: Mapping[str, Any] | None = None,
            *,
            params: Mapping[str, Any] | None = None,
        ) -> Mapping[str, Any]: ...

        def _request_json(
            self,
            method: str,
            path: str,
            *,
            payload: Mapping[str, Any] | None = None,
            params: Mapping[str, Any] | None = None,
            idempotent: bool = False,
        ) -> Mapping[str, Any]: ...

    def load_model(self, model_id: str) -> Mapping[str, Any]:
        """Load a registered model. Returns: The engine model record."""
        return self._admin("POST", "/admin/models/load", {"model_id": model_id})

    def unload_model(self, model_id: str) -> Mapping[str, Any]:
        """Unload a registered model. Returns: The engine acknowledgement."""
        return self._admin("POST", "/admin/models/unload", {"model_id": model_id})

    def list_models(self) -> Mapping[str, Any]:
        """Return the public model-discovery response without reshaping its compatibility envelope."""
        return self._request_json("GET", "/v1/models", idempotent=True)

    def model_status(self, model_id: str | None = None) -> Mapping[str, Any]:
        """Return model status. Returns: The typed JSON object supplied by the engine."""
        params = {"model_id": model_id} if model_id is not None else None
        return self._admin("GET", "/admin/models/status", params=params)

    def swap_lora(self, model_id: str, adapter_id: str | None) -> Mapping[str, Any]:
        """Swap a registered adapter on an idle loaded model.

        Args:
            model_id: Loaded base-model identifier.
            adapter_id: Registered opaque adapter ID, or ``None`` to remove LoRA.

        Returns:
            Versioned engine acknowledgement.
        """
        return self._admin("POST", "/admin/lora/swap", {"model_id": model_id, "adapter_id": adapter_id})

    def register_lora(
        self,
        adapter_id: str,
        root_id: str,
        relative_path: str,
        *,
        size_bytes: int,
        sha256: str,
        base_model_sha256: str,
        scale: float = 1.0,
    ) -> Mapping[str, Any]:
        """Register a root-confined adapter by verified content identity.

        Args:
            adapter_id: Opaque ID used by later swap requests.
            root_id: Engine-configured approved-root identifier.
            relative_path: Normalized relative path below the approved root.
            size_bytes: Exact adapter file size.
            sha256: Exact lowercase SHA-256 of the adapter file.
            base_model_sha256: Exact lowercase SHA-256 of the compatible base model.
            scale: Finite adapter scale passed to the native loader.

        Returns:
            Engine acknowledgement containing only the registered ID and digest.

        Raises:
            ValueError: If path, size, digest, or scale metadata is malformed.
        """
        normalized_parts = relative_path.replace("\\", "/").split("/")
        if (
            not relative_path
            or PurePosixPath(relative_path).is_absolute()
            or PureWindowsPath(relative_path).is_absolute()
            or any(part in {"", ".", ".."} for part in normalized_parts)
        ):
            raise ValueError("adapter relative_path must be a normalized relative path")
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes <= 0:
            raise ValueError("adapter size_bytes must be a positive integer")
        for name, digest in (("sha256", sha256), ("base_model_sha256", base_model_sha256)):
            if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
                raise ValueError(f"adapter {name} must be exactly 64 lowercase hexadecimal characters")
        if not isfinite(scale):
            raise ValueError("adapter scale must be finite")
        return self._admin(
            "POST",
            "/admin/lora/register",
            {
                "id": adapter_id,
                "root_id": root_id,
                "relative_path": relative_path,
                "size_bytes": size_bytes,
                "sha256": sha256,
                "base_model_sha256": base_model_sha256,
                "scale": scale,
            },
        )

    def set_draining(self, enabled: bool) -> Mapping[str, Any]:
        """Set the live engine's reject-new-work state explicitly."""
        return self._admin("POST", "/admin/drain", {"enabled": enabled})

    def drain(self) -> Mapping[str, Any]:
        """Reject new engine work while allowing admitted work to finish."""
        return self.set_draining(True)

    def undrain(self) -> Mapping[str, Any]:
        """Resume admission after a prior live-engine drain."""
        return self.set_draining(False)

    def reload_config(self) -> Mapping[str, Any]:
        """Refuse the unsupported success-shaped live-reload operation.

        Raises:
            EngineUnavailableError: Always; live reload is not implemented by the owned engine.
        """
        raise EngineUnavailableError(
            "AM Engine does not support live configuration reload; use EngineSupervisor.restart()"
        )

    def slots(self) -> Mapping[str, Any]:
        """Return slot status. Returns: The versioned slot snapshot."""
        return self._admin("GET", "/admin/slots")

    def register_prefix(
        self,
        prefix_name: str,
        content: str,
        *,
        content_hash: str | None = None,
        model_id: str | None = None,
    ) -> PrefixRef:
        """Register a named prefix and cache its exact engine token count.

        Args:
            prefix_name: Stable prefix name within the engine registry.
            content: Prefix text tokenized by the selected engine model.
            content_hash: Optional caller-verified SHA-256 content identity.
            model_id: Optional model whose tokenizer owns the prefix.

        Returns:
            Exact name/hash reference accepted by ``count_tokens``.

        Raises:
            EngineProtocolError: If registration omits a valid exact token count.
        """
        digest = content_hash or hashlib.sha256(content.encode("utf-8")).hexdigest()
        payload: dict[str, Any] = {
            "action": "register",
            "name": prefix_name,
            "content": content,
            "content_hash": digest,
        }
        if model_id is not None:
            payload["model"] = model_id
        response = self._admin("POST", "/admin/prefix", payload)
        raw_count = response.get("token_count", response.get("count"))
        if raw_count is None:
            raise EngineProtocolError("engine prefix registration response is missing token_count")
        if isinstance(raw_count, bool) or not isinstance(raw_count, int) or raw_count < 0:
            raise EngineProtocolError("engine prefix registration response has an invalid token_count")
        count = int(raw_count)
        with self._prefix_lock:
            stale = [key for key in self._prefix_counts if key[0] == prefix_name]
            for key in stale:
                del self._prefix_counts[key]
            self._prefix_counts[prefix_name, digest] = count
        return PrefixRef(prefix_name=prefix_name, content_hash=digest)

    def pin_prefix(self, prefix_name: str, content_hash: str) -> Mapping[str, Any]:
        """Pin a registered prefix. Returns: The engine acknowledgement."""
        return self._admin(
            "POST", "/admin/prefix", {"action": "pin", "name": prefix_name, "content_hash": content_hash}
        )

    def unpin_prefix(self, prefix_name: str, content_hash: str) -> Mapping[str, Any]:
        """Unpin a registered prefix. Returns: The engine acknowledgement."""
        return self._admin(
            "POST", "/admin/prefix", {"action": "unpin", "name": prefix_name, "content_hash": content_hash}
        )

    def create_session(self, session_id: str, model: str) -> Mapping[str, Any]:
        """Create a caller-owned, model-bound session.

        Args:
            session_id: Stable session identifier within the authenticated principal.
            model: Loaded model that owns the session KV state.

        Returns:
            Versioned engine acknowledgement.
        """
        return self._admin(
            "POST",
            "/admin/sessions",
            {"action": "create", "session_id": session_id, "model": model},
        )

    def resume_session(self, session_id: str) -> Mapping[str, Any]:
        """Resume a caller-owned session. Returns: The engine acknowledgement."""
        return self._admin("POST", "/admin/sessions", {"action": "resume", "session_id": session_id})

    def save_session(self, session_id: str) -> Mapping[str, Any]:
        """Save a caller-owned session. Returns: The engine acknowledgement."""
        return self._admin("POST", "/admin/sessions", {"action": "save", "session_id": session_id})

    def delete_session(self, session_id: str) -> Mapping[str, Any]:
        """Delete an idle caller-owned session.

        Args:
            session_id: Stable session identifier within the authenticated principal.

        Returns:
            Versioned engine acknowledgement.
        """
        return self._admin("POST", "/admin/sessions", {"action": "delete", "session_id": session_id})


__all__: list[str] = []
