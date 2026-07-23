"""Loopback protocol, owner-record, and authentication operations."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from vetinari.engine.client_types import EngineBootstrapError
from vetinari.engine.trust_anchor import EngineTrustAnchor, load_engine_trust_anchor
from vetinari.exceptions import EngineUnavailableError, EngineVersionMismatchError

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _ReceiptTrustIdentity:
    """Immutable engine installation, key, build, service, and process identity."""

    installation_id: str
    anchor_sha256: str
    key_id: str
    key_epoch: int
    algorithm: str
    provider: str
    service_identity: str
    engine_release: str
    source_commit: str
    libllama_revision: str
    release_manifest_sha256: str
    engine_binary_sha256: str
    engine_instance_id: str

    @classmethod
    def from_wire(cls, payload: object) -> _ReceiptTrustIdentity:
        if not isinstance(payload, Mapping):
            raise EngineUnavailableError("AM Engine /version response is missing receipt_trust identity")
        try:
            identity = cls(
                installation_id=_required_identity_text(payload, "installation_id"),
                anchor_sha256=_required_identity_digest(payload, "anchor_sha256"),
                key_id=_required_identity_digest(payload, "key_id"),
                key_epoch=_required_identity_epoch(payload),
                algorithm=_required_identity_text(payload, "algorithm"),
                provider=_required_identity_text(payload, "provider"),
                service_identity=_required_identity_text(payload, "service_identity"),
                engine_release=_required_identity_text(payload, "engine_release"),
                source_commit=_required_identity_text(payload, "source_commit"),
                libllama_revision=_required_identity_text(payload, "libllama_revision"),
                release_manifest_sha256=_required_identity_digest(payload, "release_manifest_sha256"),
                engine_binary_sha256=_required_identity_digest(payload, "engine_binary_sha256"),
                engine_instance_id=_required_identity_text(payload, "engine_instance_id"),
            )
        except (TypeError, ValueError) as exc:
            raise EngineUnavailableError("AM Engine /version receipt_trust identity is malformed") from exc
        return identity

    def to_wire(self) -> dict[str, object]:
        return asdict(self)


def _required_identity_text(payload: Mapping[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"invalid {field}")
    return value


def _required_identity_digest(payload: Mapping[str, Any], field: str) -> str:
    value = _required_identity_text(payload, field)
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"invalid {field}")
    return value


def _required_identity_epoch(payload: Mapping[str, Any]) -> int:
    value = payload.get("key_epoch")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("invalid key_epoch")
    return value


def _supervisor_surface():  # type: ignore[no-untyped-def]
    from vetinari.engine import supervisor

    return supervisor


class EngineProtocolMixin:
    """Validate and persist the authenticated loopback ownership contract."""

    def _prepare_receipt_trust(self) -> EngineTrustAnchor | None:
        """Verify the external anchor before exposing its paths to a child process."""
        surface = _supervisor_surface()
        if not self.config.receipt_trust_provisioned:
            self._configured_receipt_trust_anchor = None
            return None
        if (
            self.config.runtime_mode is not surface.EngineRuntimeMode.OWNED
            or self.config.binary_path is not None
            or os.environ.get(surface.ENGINE_BINARY_ENV)
            or not self._uses_default_binary_resolver
        ):
            raise EngineUnavailableError("AM Engine receipt trust is unavailable for an arbitrary binary override")
        anchor_path = self.config.receipt_trust_anchor_path
        ledger_path = self.config.receipt_ledger_path
        anchor_sha256 = self.config.receipt_anchor_sha256
        authority_pin_sha256 = self.config.receipt_authority_pin_sha256
        assert anchor_path is not None
        assert ledger_path is not None
        assert anchor_sha256 is not None
        assert authority_pin_sha256 is not None
        resolved_anchor = anchor_path.resolve()
        resolved_ledger = ledger_path.resolve()
        resolved_runtime = self.runtime_dir.resolve()
        canonical_binary = self._binary_resolver().resolve(strict=True)
        if resolved_anchor.is_relative_to(resolved_runtime):
            raise EngineUnavailableError(
                "AM Engine receipt trust anchor must be outside the mutable runtime directory",
                path=str(resolved_anchor),
            )
        if resolved_anchor.is_relative_to(canonical_binary.parent):
            raise EngineUnavailableError(
                "AM Engine receipt trust anchor must be outside the mutable engine bundle",
                path=str(resolved_anchor),
            )
        if resolved_anchor == resolved_ledger:
            raise EngineUnavailableError("AM Engine receipt anchor and ledger paths must be distinct")
        try:
            anchor = load_engine_trust_anchor(
                resolved_anchor,
                expected_anchor_sha256=anchor_sha256,
                expected_authority_pin_sha256=authority_pin_sha256,
            )
        except (OSError, TypeError, ValueError) as exc:
            raise EngineUnavailableError(
                "AM Engine receipt trust anchor could not be independently verified",
                path=str(resolved_anchor),
            ) from exc
        if anchor.is_production_eligible is not True:
            raise EngineUnavailableError(
                "AM Engine receipt trust anchor is not production eligible",
                path=str(resolved_anchor),
            )
        self._configured_receipt_trust_anchor = anchor
        return anchor

    def _adopt_pidfile_owner(self):  # type: ignore[no-untyped-def]
        surface = _supervisor_surface()
        if not self.pidfile_path.exists():
            return None
        try:
            endpoint = self._read_pidfile(strict=True)
        except EngineVersionMismatchError as exc:
            self._record_version_mismatch(exc)
            raise
        if endpoint is None:
            raise EngineUnavailableError("the AM Engine owner record is invalid; refusing a second spawn")
        if not self._pid_alive(endpoint.pid):
            self._remove_runtime_records()
            return None
        if self._handshake(endpoint, raise_on_mismatch=True):
            published_generation = max(self._endpoint_generation + 1, endpoint.generation)
            endpoint = replace(endpoint, generation=published_generation)
            self._endpoint_generation = published_generation
            self._endpoint = endpoint
            self._state = surface.EngineState.RUNNING
            self._user_message = ""
            if self.config.receipt_trust_provisioned:
                self._write_pidfile(endpoint, verified_version=self.config.expected_version)
            return endpoint
        raise EngineUnavailableError("the recorded AM Engine owner is alive but not ready", pid=endpoint.pid)

    def _handshake(self, endpoint, *, raise_on_mismatch: bool) -> bool:  # type: ignore[no-untyped-def]
        surface = _supervisor_surface()
        try:
            token = self._read_token(endpoint.token_path)
            if self.config.runtime_mode is surface.EngineRuntimeMode.OWNED:
                readiness = self._owned_readiness(endpoint, token)
                if readiness.get("control_ready") is not True:
                    return False
                if self.config.model_path is None:
                    return True
                if self._model_bootstrapped_generation != endpoint.generation:
                    self._bootstrap_configured_model(endpoint, token)
                    self._model_bootstrapped_generation = endpoint.generation
                    readiness = self._request_json(
                        endpoint.url,
                        "/readyz",
                        token,
                        self.config.request_timeout_seconds,
                        "GET",
                        None,
                    )
                    _require_owned_schema(readiness, path="/readyz")
                return readiness.get("ready") is True and readiness.get("data_ready") is True
            health = self._request_json(
                endpoint.url,
                "/health",
                token,
                self.config.request_timeout_seconds,
                "GET",
                None,
            )
            return str(health.get("status", "")).lower() in {"ok", "ready", "healthy"}
        except EngineBootstrapError:
            self._state = surface.EngineState.DEGRADED
            self._user_message = "AM Engine configured model bootstrap failed; inspect the typed error and startup logs"
            raise
        except EngineVersionMismatchError as exc:
            if raise_on_mismatch:
                self._record_version_mismatch(exc)
                raise
            logger.warning(
                "AM Engine handshake rejected a version mismatch during a non-raising probe: %s",
                exc,
            )
            return False
        except EngineUnavailableError as exc:
            if self.config.receipt_trust_provisioned:
                self._receipt_trust_identity = None
                self._receipt_engine_instance_id = None
                if raise_on_mismatch:
                    raise
            logger.warning("AM Engine handshake is not ready: %s", exc)
            return False
        except (
            OSError,
            ValueError,
            surface.httpx.HTTPError,
            json.JSONDecodeError,
        ) as exc:
            logger.warning("AM Engine handshake is not ready: %s", exc)
            return False

    def _probe_endpoint(self, endpoint) -> bool:  # type: ignore[no-untyped-def]
        surface = _supervisor_surface()
        try:
            token = self._read_token(endpoint.token_path)
            if self.config.runtime_mode is surface.EngineRuntimeMode.OWNED:
                readiness = self._owned_readiness(endpoint, token)
                if readiness.get("control_ready") is not True:
                    return False
                if self.config.model_path is None:
                    return True
                return readiness.get("ready") is True and readiness.get("data_ready") is True
            health = self._request_json(
                endpoint.url,
                "/health",
                token,
                self.config.request_timeout_seconds,
                "GET",
                None,
            )
            return str(health.get("status", "")).lower() in {"ok", "ready", "healthy"}
        except EngineVersionMismatchError:
            raise
        except (
            OSError,
            ValueError,
            surface.httpx.HTTPError,
            json.JSONDecodeError,
            EngineUnavailableError,
        ) as exc:
            logger.warning("AM Engine status probe is not ready: %s", exc)
            return False

    def _owned_readiness(self, endpoint, token: str) -> Mapping[str, Any]:  # type: ignore[no-untyped-def]
        version = self._request_json(endpoint.url, "/version", token, self.config.request_timeout_seconds, "GET", None)
        _require_owned_schema(version, path="/version")
        observed = version.get("engine_version")
        if observed != self.config.expected_version:
            raise self._version_mismatch_error(str(observed), action="reinstall the pinned release")
        if not isinstance(version.get("libllama_rev"), str) or not version["libllama_rev"]:
            raise EngineUnavailableError("AM Engine /version response is missing libllama_rev")
        if self.config.receipt_trust_provisioned:
            self._receipt_trust_identity = None
            self._receipt_engine_instance_id = None
            anchor = self._configured_receipt_trust_anchor or self._prepare_receipt_trust()
            assert anchor is not None
            identity = _ReceiptTrustIdentity.from_wire(version.get("receipt_trust"))
            expected_identity = _ReceiptTrustIdentity(
                installation_id=anchor.installation_id,
                anchor_sha256=anchor.anchor_sha256,
                key_id=anchor.key_id,
                key_epoch=anchor.key_epoch,
                algorithm=anchor.algorithm,
                provider=anchor.provider,
                service_identity=anchor.service_identity,
                engine_release=anchor.engine_release,
                source_commit=anchor.source_commit,
                libllama_revision=anchor.libllama_revision,
                release_manifest_sha256=anchor.release_manifest_sha256,
                engine_binary_sha256=anchor.engine_binary_sha256,
                engine_instance_id=identity.engine_instance_id,
            )
            if identity != expected_identity:
                raise EngineUnavailableError(
                    "AM Engine /version receipt identity does not match the independently pinned trust anchor"
                )
            if version["libllama_rev"] != identity.libllama_revision:
                raise EngineUnavailableError(
                    "AM Engine /version libllama identities disagree",
                    observed=version["libllama_rev"],
                    expected=identity.libllama_revision,
                )
            if self._owner_receipt_trust_identity is not None and identity != self._owner_receipt_trust_identity:
                raise EngineUnavailableError(
                    "AM Engine /version receipt identity changed from the immutable owner record"
                )
            self._receipt_trust_identity = identity
            self._receipt_engine_instance_id = identity.engine_instance_id
        readiness = self._request_json(endpoint.url, "/readyz", token, self.config.request_timeout_seconds, "GET", None)
        _require_owned_schema(readiness, path="/readyz")
        return readiness

    def _bootstrap_configured_model(self, endpoint, token: str) -> None:  # type: ignore[no-untyped-def]
        surface = _supervisor_surface()
        assert self.config.model_path is not None
        model_id = self.config.model_path.stem
        try:
            model_id = surface._configured_model_id(self.config.model_path)
            loaded = self._request_json(
                endpoint.url,
                "/admin/models/load",
                token,
                self.config.request_timeout_seconds,
                "POST",
                {"schema_version": 1, "model_id": model_id},
            )
            _require_owned_schema(loaded, path="/admin/models/load")
            if loaded.get("loaded") != model_id:
                raise EngineUnavailableError(
                    "AM Engine model bootstrap acknowledgement did not match the configured model",
                    model_id=model_id,
                )
        except (
            OSError,
            ValueError,
            surface.httpx.HTTPError,
            json.JSONDecodeError,
            EngineUnavailableError,
        ) as exc:
            raise EngineBootstrapError(
                "AM Engine failed to bootstrap the configured model",
                model_id=model_id,
                generation=endpoint.generation,
            ) from exc

    def _read_pidfile(self, *, strict: bool = False):  # type: ignore[no-untyped-def]
        surface = _supervisor_surface()
        try:
            payload = json.loads(self.pidfile_path.read_text(encoding="utf-8"))
            owner_schema = payload["schema_version"]
            accepted_schema = (
                {"vetinari-engine-owner.v3", "vetinari-engine-owner.starting.v1"}
                if self.config.receipt_trust_provisioned
                else {"vetinari-engine-owner.v2"}
            )
            if owner_schema not in accepted_schema:
                raise ValueError("unsupported owner-record schema")
            pid, port = int(payload["pid"]), int(payload["port"])
            host, token_path = str(payload["host"]), Path(str(payload["token_path"])).resolve()
            policy_path = Path(str(payload.get("auth_policy_path", self.auth_policy_path))).resolve()
            if pid <= 0 or not (1 <= port <= 65535) or host not in {"127.0.0.1", "localhost", "::1"}:
                raise ValueError("owner-record endpoint is invalid")
            if token_path != self.token_path.resolve():
                raise ValueError("owner-record token path is outside this runtime directory")
            if policy_path != self.auth_policy_path.resolve():
                raise ValueError("owner-record auth policy path is outside this runtime directory")
            if str(payload["runtime_mode"]) != self.config.runtime_mode.value:
                raise ValueError("owner-record runtime mode does not match this supervisor")
            verified_version = str(payload["verified_version"])
            if (
                verified_version != self.config.expected_version
                or payload["expected_version"] != self.config.expected_version
            ):
                raise self._version_mismatch_error(verified_version, action="refusing owner adoption")
            generation = int(payload.get("generation", 0))
            if generation < 0:
                raise ValueError("owner-record generation is invalid")
            self._owner_receipt_trust_identity = None
            if self.config.receipt_trust_provisioned and owner_schema == "vetinari-engine-owner.v3":
                if payload.get("receipt_anchor_sha256") != self.config.receipt_anchor_sha256:
                    raise ValueError("owner-record receipt anchor pin does not match this supervisor")
                if payload.get("receipt_authority_pin_sha256") != self.config.receipt_authority_pin_sha256:
                    raise ValueError("owner-record receipt authority pin does not match this supervisor")
                self._owner_receipt_trust_identity = _ReceiptTrustIdentity.from_wire(
                    payload.get("receipt_trust_identity")
                )
            return surface.EngineEndpoint(pid=pid, host=host, port=port, token_path=token_path, generation=generation)
        except FileNotFoundError as exc:
            logger.debug("AM Engine pidfile is absent: %s", exc)
            return None
        except EngineVersionMismatchError as exc:
            if strict:
                raise
            logger.warning("AM Engine pidfile version is invalid: %s", exc)
            return None
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError, EngineUnavailableError) as exc:
            if strict:
                raise EngineUnavailableError(
                    "the AM Engine owner record is invalid; refusing a second spawn",
                    path=str(self.pidfile_path),
                ) from exc
            logger.warning("AM Engine pidfile is unavailable or invalid: %s", exc)
            return None

    def _write_pidfile(self, endpoint, *, verified_version: str) -> None:  # type: ignore[no-untyped-def]
        receipt_identity = self._receipt_trust_identity
        if self.config.receipt_trust_provisioned and receipt_identity is None:
            schema_version = "vetinari-engine-owner.starting.v1"
        elif self.config.receipt_trust_provisioned:
            schema_version = "vetinari-engine-owner.v3"
        else:
            schema_version = "vetinari-engine-owner.v2"
        payload = {
            "schema_version": schema_version,
            "pid": endpoint.pid,
            "host": endpoint.host,
            "port": endpoint.port,
            "endpoint": endpoint.url,
            "generation": endpoint.generation,
            "token_path": str(endpoint.token_path.resolve()),
            "auth_policy_path": str(self.auth_policy_path.resolve()),
            "runtime_mode": self.config.runtime_mode.value,
            "expected_version": self.config.expected_version,
            "verified_version": verified_version,
        }
        if isinstance(receipt_identity, _ReceiptTrustIdentity):
            payload.update({
                "receipt_anchor_sha256": self.config.receipt_anchor_sha256,
                "receipt_authority_pin_sha256": self.config.receipt_authority_pin_sha256,
                "receipt_trust_identity": receipt_identity.to_wire(),
            })
        staged = self.pidfile_path.with_suffix(".tmp")
        staged.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        os.replace(staged, self.pidfile_path)

    def _remove_runtime_records(self) -> None:
        self._receipt_trust_identity = None
        self._receipt_engine_instance_id = None
        self._owner_receipt_trust_identity = None
        for path in (self.pidfile_path, self.token_path, self.auth_policy_path):
            try:
                path.unlink()
            except FileNotFoundError:
                logger.debug("AM Engine runtime record already absent: %s", path)

    def _write_auth_policy(self, token: str) -> None:
        surface = _supervisor_surface()
        token_bytes = token.encode("utf-8")
        if not 1 <= len(token_bytes) <= surface._MAX_AUTH_TOKEN_BYTES:
            raise EngineUnavailableError("AM Engine authentication token length is invalid")
        payload = {
            "schema_version": 2,
            "credentials": [
                {
                    "principal_id": surface._LOCAL_SUPERVISOR_PRINCIPAL,
                    "token_sha256": hashlib.sha256(token_bytes).hexdigest(),
                    "token_byte_length": len(token_bytes),
                    "scopes": ["inference", "observability", "admin"],
                }
            ],
        }
        surface._write_private_text(
            self.auth_policy_path,
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
        )

    @staticmethod
    def _read_token(path: Path) -> str:
        surface = _supervisor_surface()
        try:
            token = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise EngineUnavailableError("AM Engine authentication token is unavailable", path=str(path)) from exc
        if not token:
            raise EngineUnavailableError("AM Engine authentication token is empty", path=str(path))
        if len(token.encode("utf-8")) > surface._MAX_AUTH_TOKEN_BYTES:
            raise EngineUnavailableError("AM Engine authentication token length is invalid", path=str(path))
        return token

    @staticmethod
    def _default_request_json(
        base_url: str,
        path: str,
        token: str,
        timeout: float,
        method: str,
        body: Mapping[str, Any] | None,
    ) -> Mapping[str, Any]:
        surface = _supervisor_surface()
        url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
        parsed = urlparse(url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise EngineUnavailableError("AM Engine endpoint must be loopback HTTP", endpoint=base_url)
        response = surface.httpx.request(
            method,
            url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            json=dict(body) if body is not None else None,
            timeout=timeout,
            trust_env=False,
        )
        if not (path == "/readyz" and response.status_code == surface.httpx.codes.SERVICE_UNAVAILABLE):
            response.raise_for_status()
        if len(response.content) > 1024 * 1024:
            raise EngineUnavailableError("AM Engine response exceeded the size limit", path=path)
        payload = response.json()
        if not isinstance(payload, dict):
            raise EngineUnavailableError("AM Engine returned a non-object response", path=path)
        return payload


def _require_owned_schema(payload: Mapping[str, Any], *, path: str) -> None:
    """Reject a malformed first-party lifecycle response."""
    raw = payload.get("schema_version")
    try:
        major = int(str(raw).split(".", maxsplit=1)[0])
    except (TypeError, ValueError) as exc:
        raise EngineUnavailableError(
            "AM Engine lifecycle response has a malformed schema version",
            path=path,
            observed=raw,
        ) from exc
    if major != 1:
        raise EngineUnavailableError(
            "AM Engine lifecycle response uses an unsupported schema major",
            path=path,
            expected=1,
            observed=raw,
        )
