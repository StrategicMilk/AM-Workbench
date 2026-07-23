"""Verification of independently pinned AM Engine installation trust anchors."""

from __future__ import annotations

import base64
import json
import re
import stat
import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from itertools import starmap
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils
from cryptography.hazmat.primitives.serialization import load_der_public_key

from vetinari.engine.binary import release_authority_receipt
from vetinari.engine.client_types import EngineProtocolError
from vetinari.exceptions import EngineBinaryMissingError

_ANCHOR_SCHEMA_VERSION = 2
_SIGNATURE_ALGORITHM = "ecdsa-p256-sha256-p1363"
_ANCHOR_V1_DOMAIN = b"AMW\x00engine-installation-trust-anchor-v1\x00"
_ANCHOR_V2_DOMAIN = b"AMW\x00engine-installation-trust-anchor-v2\x00"
_P256_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
_P256_HALF_ORDER = _P256_ORDER // 2
_MAX_ANCHOR_BYTES = 64 * 1024
_HEX_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_BASE64URL_RE = re.compile(r"[A-Za-z0-9_-]+\Z")
_RFC3339_UTC_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z\Z")
_PRODUCTION_PROVIDERS = frozenset({"windows_cng_machine", "tpm", "pkcs11", "hsm"})
_TEST_PROVIDER = "software_test"
_PRODUCTION_TRUST = "production_protected"
_TEST_TRUST = "untrusted_software_test"
_ANCHOR_V1_KEYS = frozenset({
    "schema_version",
    "installation_id",
    "key_id",
    "key_epoch",
    "algorithm",
    "public_key_spki_der",
    "provider",
    "trust",
    "service_identity",
    "engine_release",
    "source_commit",
    "libllama_revision",
    "release_manifest_sha256",
    "engine_binary_sha256",
    "authenticode_signer_identity",
    "created_at",
    "predecessor_key_id",
    "predecessor_key_epoch",
    "authority_key_id",
    "authority_public_key_spki_der",
    "proof_of_possession",
    "authority_signature",
})
_ANCHOR_V2_KEYS = _ANCHOR_V1_KEYS | {"predecessor_anchor_sha256"}
_MAX_ANCHOR_CHAIN_DEPTH = 64


class EngineTrustAnchorError(EngineProtocolError):
    """An engine installation trust anchor failed closed validation."""


@dataclass(frozen=True, slots=True)
class EngineTrustAnchor:
    """Authenticated bindings for one protected AM Engine installation key."""

    schema_version: int
    installation_id: str
    anchor_sha256: str
    key_id: str
    key_epoch: int
    algorithm: str
    public_key_spki_der: bytes
    provider: str
    trust: str
    service_identity: str
    engine_release: str
    source_commit: str
    libllama_revision: str
    release_manifest_sha256: str
    engine_binary_sha256: str
    authenticode_signer_identity: str | None
    created_at: str
    predecessor_key_id: str | None
    predecessor_key_epoch: int | None
    predecessor_anchor_sha256: str | None
    authority_key_id: str
    is_production_eligible: bool

    def __repr__(self) -> str:
        return (
            f"EngineTrustAnchor(installation_id={self.installation_id!r}, key_id={self.key_id!r}, "
            f"key_epoch={self.key_epoch!r}, provider={self.provider!r}, "
            f"production_eligible={self.is_production_eligible!r})"
        )


def load_engine_trust_anchor(
    anchor_path: Path,
    *,
    expected_anchor_sha256: str,
    expected_authority_pin_sha256: str,
    allow_untrusted_test_signer: bool = False,
) -> EngineTrustAnchor:
    """Load and authenticate one independently pinned installation anchor.

    Args:
        anchor_path: Protected installer or service-state file. The caller must
            source this path outside the mutable inner engine bundle.
        expected_anchor_sha256: Independently provisioned digest of the exact
            anchor file bytes.
        expected_authority_pin_sha256: Independently provisioned SHA-256 digest
            of the P155 authority SubjectPublicKeyInfo DER.
        allow_untrusted_test_signer: Permit a software-test anchor to load while
            retaining its ineligible trust classification.

    Returns:
        The fully authenticated installation and engine-build bindings.

    Raises:
        EngineTrustAnchorError: If the file, schema, pins, keys, provider, or
            either required P-256 signature is invalid.
    """
    _require_digest("expected_anchor_sha256", expected_anchor_sha256)
    _require_digest("expected_authority_pin_sha256", expected_authority_pin_sha256)
    raw = _read_anchor_bytes(anchor_path)
    anchor_sha256 = sha256(raw).hexdigest()
    if anchor_sha256 != expected_anchor_sha256:
        raise EngineTrustAnchorError("engine trust anchor does not match the independently provisioned anchor pin")
    payload = _parse_anchor_json(raw)
    parsed = _parse_anchor_fields(payload)
    authority_der = parsed["authority_public_key_spki_der"]
    authority_key_id = sha256(authority_der).hexdigest()
    if authority_key_id != expected_authority_pin_sha256 or parsed["authority_key_id"] != authority_key_id:
        raise EngineTrustAnchorError(
            "engine trust anchor authority key does not match the independently provisioned pin"
        )
    engine_der = parsed["public_key_spki_der"]
    if parsed["key_id"] != sha256(engine_der).hexdigest():
        raise EngineTrustAnchorError("engine trust anchor key_id does not match its SubjectPublicKeyInfo")
    statement = _canonical_anchor_statement(parsed)
    engine_key = _load_p256_public_key(engine_der, field="public_key_spki_der")
    authority_key = _load_p256_public_key(authority_der, field="authority_public_key_spki_der")
    _verify_p1363_low_s(engine_key, parsed["proof_of_possession"], statement, field="proof_of_possession")
    _verify_p1363_low_s(authority_key, parsed["authority_signature"], statement, field="authority_signature")
    production_eligible = _validate_provider_trust(
        parsed["provider"],
        parsed["trust"],
        authenticode_signer_identity=parsed["authenticode_signer_identity"],
        allow_untrusted_test_signer=allow_untrusted_test_signer,
    )
    if production_eligible:
        _require_pinned_release_identity(parsed)
    return EngineTrustAnchor(
        schema_version=parsed["schema_version"],
        installation_id=parsed["installation_id"],
        anchor_sha256=anchor_sha256,
        key_id=parsed["key_id"],
        key_epoch=parsed["key_epoch"],
        algorithm=parsed["algorithm"],
        public_key_spki_der=engine_der,
        provider=parsed["provider"],
        trust=parsed["trust"],
        service_identity=parsed["service_identity"],
        engine_release=parsed["engine_release"],
        source_commit=parsed["source_commit"],
        libllama_revision=parsed["libllama_revision"],
        release_manifest_sha256=parsed["release_manifest_sha256"],
        engine_binary_sha256=parsed["engine_binary_sha256"],
        authenticode_signer_identity=parsed["authenticode_signer_identity"],
        created_at=parsed["created_at"],
        predecessor_key_id=parsed["predecessor_key_id"],
        predecessor_key_epoch=parsed["predecessor_key_epoch"],
        predecessor_anchor_sha256=parsed["predecessor_anchor_sha256"],
        authority_key_id=authority_key_id,
        is_production_eligible=production_eligible,
    )


def _require_pinned_release_identity(parsed: Mapping[str, Any]) -> None:
    """Bind a production anchor to the package-owned immutable release pins."""
    try:
        authority = release_authority_receipt()
    except EngineBinaryMissingError as exc:
        raise EngineTrustAnchorError("independently pinned engine release authority is unavailable") from exc
    expected_release = str(authority["release"]).removeprefix("v")
    if (
        parsed["engine_release"] != expected_release
        or parsed["source_commit"] != authority["source_commit"]
        or parsed["release_manifest_sha256"] != authority["release_manifest_sha256"]
    ):
        raise EngineTrustAnchorError(
            "engine trust anchor release identity does not match the independently pinned release authority"
        )


def resolve_engine_trust_anchor(
    trust_context: tuple[Path, str, str],
    *,
    requested_anchor_sha256: str,
    allow_untrusted_test_signer: bool = False,
) -> EngineTrustAnchor:
    """Resolve a current or retained predecessor anchor for persisted evidence.

    Args:
        trust_context: Supervisor-owned current anchor path, exact current
            anchor digest, and independently pinned P155 authority SPKI digest.
        requested_anchor_sha256: Exact anchor digest signed into the persisted
            receipt claims.
        allow_untrusted_test_signer: Permit test-only software anchors while
            preserving their ineligible classification.

    Returns:
        The current anchor or an authenticated predecessor linked to it.

    Raises:
        EngineTrustAnchorError: If the context, archive path, chain, file,
            authority, signatures, or requested digest fails closed validation.
    """
    if not isinstance(trust_context, tuple) or len(trust_context) != 3:
        raise EngineTrustAnchorError("engine receipt trust context must contain path, anchor pin, and authority pin")
    anchor_path, current_anchor_sha256, authority_pin_sha256 = trust_context
    if not isinstance(anchor_path, Path):
        raise EngineTrustAnchorError("engine receipt trust context anchor path must be a Path")
    _require_digest("current_anchor_sha256", current_anchor_sha256)
    _require_digest("authority_pin_sha256", authority_pin_sha256)
    _require_digest("requested_anchor_sha256", requested_anchor_sha256)
    current = load_engine_trust_anchor(
        anchor_path,
        expected_anchor_sha256=current_anchor_sha256,
        expected_authority_pin_sha256=authority_pin_sha256,
        allow_untrusted_test_signer=allow_untrusted_test_signer,
    )
    if requested_anchor_sha256 == current.anchor_sha256:
        return current
    archive_root = anchor_path.with_name(f"{anchor_path.name}.archive")
    _validate_archive_root(archive_root)
    seen = {current.anchor_sha256}
    descendant = current
    for _depth in range(_MAX_ANCHOR_CHAIN_DEPTH):
        predecessor_digest = descendant.predecessor_anchor_sha256
        if predecessor_digest is None:
            raise EngineTrustAnchorError(
                "requested engine trust anchor is not retained in the current predecessor chain"
            )
        if predecessor_digest in seen:
            raise EngineTrustAnchorError("engine trust anchor predecessor chain contains a cycle")
        seen.add(predecessor_digest)
        predecessor_path = archive_root / f"{predecessor_digest}.json"
        predecessor = load_engine_trust_anchor(
            predecessor_path,
            expected_anchor_sha256=predecessor_digest,
            expected_authority_pin_sha256=authority_pin_sha256,
            allow_untrusted_test_signer=allow_untrusted_test_signer,
        )
        _validate_predecessor_link(descendant, predecessor)
        if predecessor.anchor_sha256 == requested_anchor_sha256:
            return predecessor
        descendant = predecessor
    raise EngineTrustAnchorError("engine trust anchor predecessor chain exceeds the supported depth")


def _read_anchor_bytes(path: Path) -> bytes:
    try:
        if path.is_symlink():
            raise EngineTrustAnchorError("engine trust anchor path must not be a symbolic link")
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode):
            raise EngineTrustAnchorError("engine trust anchor path must name a regular file")
        if metadata.st_size <= 0 or metadata.st_size > _MAX_ANCHOR_BYTES:
            raise EngineTrustAnchorError("engine trust anchor size is outside the accepted range")
        raw = path.read_bytes()
    except EngineTrustAnchorError:
        raise
    except OSError as exc:
        raise EngineTrustAnchorError("engine trust anchor is missing or unreadable") from exc
    if len(raw) != metadata.st_size:
        raise EngineTrustAnchorError("engine trust anchor changed while it was being read")
    return raw


def _parse_anchor_json(raw: bytes) -> Mapping[str, Any]:
    try:
        text = raw.decode("utf-8", errors="strict")
        payload = json.loads(text, object_pairs_hook=_object_without_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise EngineTrustAnchorError("engine trust anchor is not strict duplicate-free UTF-8 JSON") from exc
    if not isinstance(payload, Mapping):
        raise EngineTrustAnchorError("engine trust anchor must be a JSON object")
    schema_version = payload.get("schema_version")
    if isinstance(schema_version, bool) or schema_version not in {1, _ANCHOR_SCHEMA_VERSION}:
        raise EngineTrustAnchorError("engine trust anchor schema_version is unsupported")
    expected_keys = _ANCHOR_V1_KEYS if schema_version == 1 else _ANCHOR_V2_KEYS
    if set(payload) != expected_keys:
        raise EngineTrustAnchorError(f"engine trust anchor fields do not match schema version {schema_version}")
    return payload


def _object_without_duplicates(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _parse_anchor_fields(payload: Mapping[str, Any]) -> dict[str, Any]:
    parsed = dict(payload)
    parsed["installation_id"] = _require_text("installation_id", payload["installation_id"])
    parsed["key_id"] = _require_digest("key_id", payload["key_id"])
    parsed["key_epoch"] = _require_uint("key_epoch", payload["key_epoch"], bits=64, positive=True)
    if payload["algorithm"] != _SIGNATURE_ALGORITHM:
        raise EngineTrustAnchorError("engine trust anchor algorithm is unsupported")
    parsed["public_key_spki_der"] = _decode_base64url("public_key_spki_der", payload["public_key_spki_der"])
    parsed["provider"] = _require_text("provider", payload["provider"])
    parsed["trust"] = _require_text("trust", payload["trust"])
    for field in ("service_identity", "engine_release", "source_commit", "libllama_revision"):
        parsed[field] = _require_text(field, payload[field])
    for field in ("release_manifest_sha256", "engine_binary_sha256", "authority_key_id"):
        parsed[field] = _require_digest(field, payload[field])
    parsed["authenticode_signer_identity"] = _optional_text(
        "authenticode_signer_identity", payload["authenticode_signer_identity"]
    )
    created_at = _require_text("created_at", payload["created_at"])
    if not _RFC3339_UTC_RE.fullmatch(created_at):
        raise EngineTrustAnchorError("engine trust anchor created_at must be an RFC3339 UTC timestamp")
    parsed["created_at"] = created_at
    parsed["predecessor_key_id"] = _optional_digest("predecessor_key_id", payload["predecessor_key_id"])
    parsed["predecessor_key_epoch"] = _optional_uint("predecessor_key_epoch", payload["predecessor_key_epoch"], bits=64)
    parsed["predecessor_anchor_sha256"] = (
        _optional_digest("predecessor_anchor_sha256", payload["predecessor_anchor_sha256"])
        if payload["schema_version"] == _ANCHOR_SCHEMA_VERSION
        else None
    )
    _validate_rotation(parsed)
    parsed["authority_public_key_spki_der"] = _decode_base64url(
        "authority_public_key_spki_der", payload["authority_public_key_spki_der"]
    )
    parsed["proof_of_possession"] = _decode_base64url("proof_of_possession", payload["proof_of_possession"])
    parsed["authority_signature"] = _decode_base64url("authority_signature", payload["authority_signature"])
    return parsed


def _validate_rotation(parsed: Mapping[str, Any]) -> None:
    predecessor_id = parsed["predecessor_key_id"]
    predecessor_epoch = parsed["predecessor_key_epoch"]
    predecessor_anchor = parsed["predecessor_anchor_sha256"]
    if (predecessor_id is None) != (predecessor_epoch is None):
        raise EngineTrustAnchorError("engine trust anchor predecessor key id and epoch must be supplied together")
    if parsed["key_epoch"] == 1 and predecessor_id is not None:
        raise EngineTrustAnchorError("engine trust anchor epoch 1 must not declare a predecessor")
    if parsed["key_epoch"] == 1 and predecessor_anchor is not None:
        raise EngineTrustAnchorError("engine trust anchor epoch 1 must not declare a predecessor anchor")
    if parsed["key_epoch"] > 1 and (predecessor_id is None or predecessor_epoch is None):
        raise EngineTrustAnchorError("rotated engine trust anchors must declare a predecessor")
    if predecessor_epoch is not None and predecessor_epoch >= parsed["key_epoch"]:
        raise EngineTrustAnchorError("engine trust anchor predecessor epoch must be older than key_epoch")
    if parsed["schema_version"] == _ANCHOR_SCHEMA_VERSION and (
        (predecessor_id is None) != (predecessor_anchor is None)
    ):
        raise EngineTrustAnchorError(
            "engine trust anchor predecessor key and predecessor anchor digest must be supplied together"
        )


def _validate_provider_trust(
    provider: str,
    trust: str,
    *,
    authenticode_signer_identity: str | None,
    allow_untrusted_test_signer: bool,
) -> bool:
    if provider in _PRODUCTION_PROVIDERS and trust == _PRODUCTION_TRUST:
        if provider == "windows_cng_machine" and authenticode_signer_identity is None:
            raise EngineTrustAnchorError("Windows production anchors require an Authenticode signer identity")
        return True
    if provider == _TEST_PROVIDER and trust == _TEST_TRUST:
        if not allow_untrusted_test_signer:
            raise EngineTrustAnchorError("software-test engine receipt signers are explicitly untrusted")
        return False
    raise EngineTrustAnchorError(
        "engine trust anchor provider and trust classification are inconsistent or unsupported"
    )


def _canonical_anchor_statement(parsed: Mapping[str, Any]) -> bytes:
    predecessor_key = bytes(32) if parsed["predecessor_key_id"] is None else bytes.fromhex(parsed["predecessor_key_id"])
    predecessor_epoch = 0 if parsed["predecessor_key_epoch"] is None else parsed["predecessor_key_epoch"]
    authenticode = parsed["authenticode_signer_identity"] or ""
    values = [
        struct.pack(">H", parsed["schema_version"]),
        parsed["installation_id"].encode(),
        bytes.fromhex(parsed["key_id"]),
        struct.pack(">Q", parsed["key_epoch"]),
        parsed["algorithm"].encode(),
        parsed["public_key_spki_der"],
        parsed["provider"].encode(),
        parsed["trust"].encode(),
        parsed["service_identity"].encode(),
        parsed["engine_release"].encode(),
        parsed["source_commit"].encode(),
        parsed["libllama_revision"].encode(),
        bytes.fromhex(parsed["release_manifest_sha256"]),
        bytes.fromhex(parsed["engine_binary_sha256"]),
        authenticode.encode(),
        parsed["created_at"].encode(),
        predecessor_key,
        struct.pack(">Q", predecessor_epoch),
    ]
    if parsed["schema_version"] == _ANCHOR_SCHEMA_VERSION:
        predecessor_anchor = (
            bytes(32)
            if parsed["predecessor_anchor_sha256"] is None
            else bytes.fromhex(parsed["predecessor_anchor_sha256"])
        )
        values.append(predecessor_anchor)
    values.extend((bytes.fromhex(parsed["authority_key_id"]), parsed["authority_public_key_spki_der"]))
    domain = _ANCHOR_V1_DOMAIN if parsed["schema_version"] == 1 else _ANCHOR_V2_DOMAIN
    return domain + b"".join(starmap(_tlv, enumerate(values, start=1)))


def _validate_archive_root(archive_root: Path) -> None:
    try:
        if archive_root.is_symlink():
            raise EngineTrustAnchorError("engine trust anchor archive directory must not be a symbolic link")
        metadata = archive_root.stat()
    except EngineTrustAnchorError:
        raise
    except OSError as exc:
        raise EngineTrustAnchorError("engine trust anchor predecessor archive is missing or unreadable") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise EngineTrustAnchorError("engine trust anchor predecessor archive must be a directory")


def _validate_predecessor_link(descendant: EngineTrustAnchor, predecessor: EngineTrustAnchor) -> None:
    if descendant.schema_version != _ANCHOR_SCHEMA_VERSION:
        raise EngineTrustAnchorError("legacy engine trust anchors cannot select a predecessor archive entry")
    if (
        descendant.predecessor_anchor_sha256 != predecessor.anchor_sha256
        or descendant.predecessor_key_id != predecessor.key_id
        or descendant.predecessor_key_epoch != predecessor.key_epoch
    ):
        raise EngineTrustAnchorError("engine trust anchor archive entry does not match the signed predecessor relation")
    if descendant.installation_id != predecessor.installation_id:
        raise EngineTrustAnchorError("engine trust anchor predecessor belongs to a different installation")
    if descendant.authority_key_id != predecessor.authority_key_id:
        raise EngineTrustAnchorError("engine trust anchor predecessor uses a substituted P155 authority")


def _tlv(tag: int, value: bytes) -> bytes:
    return struct.pack(">HI", tag, len(value)) + value


def _load_p256_public_key(der: bytes, *, field: str) -> ec.EllipticCurvePublicKey:
    try:
        key = load_der_public_key(der)
    except (TypeError, ValueError) as exc:
        raise EngineTrustAnchorError(f"engine trust anchor {field} is not valid SubjectPublicKeyInfo DER") from exc
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        raise EngineTrustAnchorError(f"engine trust anchor {field} must contain a P-256 public key")
    return key


def _verify_p1363_low_s(
    key: ec.EllipticCurvePublicKey,
    signature: bytes,
    message: bytes,
    *,
    field: str,
) -> None:
    if len(signature) != 64:
        raise EngineTrustAnchorError(f"engine trust anchor {field} must be a 64-byte P1363 signature")
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:], "big")
    if not 1 <= r < _P256_ORDER or not 1 <= s <= _P256_HALF_ORDER:
        raise EngineTrustAnchorError(f"engine trust anchor {field} must use valid low-S P-256 scalars")
    try:
        key.verify(utils.encode_dss_signature(r, s), message, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature as exc:
        raise EngineTrustAnchorError(f"engine trust anchor {field} is invalid") from exc


def _decode_base64url(field: str, value: Any) -> bytes:
    if not isinstance(value, str) or not _BASE64URL_RE.fullmatch(value) or "=" in value:
        raise EngineTrustAnchorError(f"engine trust anchor {field} must be unpadded base64url")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, base64.binascii.Error) as exc:
        raise EngineTrustAnchorError(f"engine trust anchor {field} is invalid base64url") from exc
    if base64.urlsafe_b64encode(decoded).rstrip(b"=").decode() != value:
        raise EngineTrustAnchorError(f"engine trust anchor {field} is not canonical base64url")
    return decoded


def _require_digest(field: str, value: Any) -> str:
    if not isinstance(value, str) or not _HEX_DIGEST_RE.fullmatch(value):
        raise EngineTrustAnchorError(f"engine trust anchor {field} must be a lowercase SHA-256 digest")
    return value


def _optional_digest(field: str, value: Any) -> str | None:
    return None if value is None else _require_digest(field, value)


def _require_text(field: str, value: Any) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 4096 or "\x00" in value:
        raise EngineTrustAnchorError(f"engine trust anchor {field} must be a bounded non-empty UTF-8 string")
    return value


def _optional_text(field: str, value: Any) -> str | None:
    return None if value is None else _require_text(field, value)


def _require_uint(field: str, value: Any, *, bits: int, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < int(positive) or value >= 1 << bits:
        raise EngineTrustAnchorError(f"engine trust anchor {field} must be an unsigned {bits}-bit integer")
    return value


def _optional_uint(field: str, value: Any, *, bits: int) -> int | None:
    return None if value is None else _require_uint(field, value, bits=bits)


__all__ = [
    "EngineTrustAnchor",
    "EngineTrustAnchorError",
    "load_engine_trust_anchor",
    "resolve_engine_trust_anchor",
]
