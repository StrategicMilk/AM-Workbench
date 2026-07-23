"""Strict verification of Rust-authored AM Engine evaluation receipts."""

from __future__ import annotations

import base64
import re
import struct
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import sha256
from itertools import starmap
from pathlib import Path
from threading import Lock
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils
from cryptography.hazmat.primitives.serialization import load_der_public_key

from vetinari.engine.client_types import EngineProtocolError
from vetinari.engine.trust_anchor import EngineTrustAnchor, resolve_engine_trust_anchor

_RECEIPT_DOMAIN = b"AMW\x00engine-eval-terminal-receipt\x00"
_ATTEMPT_DOMAIN = b"AMW\x00engine-eval-attempt\x00"
_CANONICALIZATION = "amw-eval-tlv-v1"
_ALGORITHM = "ecdsa-p256-sha256-p1363"
_P256_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
_P256_HALF_ORDER = _P256_ORDER // 2
_HEX_RE = re.compile(r"[0-9a-f]{64}\Z")
_BASE64URL_RE = re.compile(r"[A-Za-z0-9_-]+\Z")
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_TOP_KEYS = frozenset({"canonicalization", "algorithm", "claims", "receipt_id", "signature", "signer"})
_SIGNER_KEYS = frozenset({"provider", "trust", "public_key_spki_der"})
_FIELDS = (
    ("schema_version", "u16"),
    ("installation_id", "string"),
    ("anchor_sha256", "digest32"),
    ("key_id", "digest32"),
    ("key_epoch", "u64"),
    ("engine_release", "string"),
    ("source_commit", "string"),
    ("libllama_revision", "string"),
    ("release_manifest_sha256", "digest32"),
    ("engine_binary_sha256", "digest32"),
    ("engine_instance_id", "string"),
    ("principal_id", "string"),
    ("request_id", "string"),
    ("trace_id", "string"),
    ("endpoint", "string"),
    ("run_id", "string"),
    ("suite_id", "string"),
    ("suite_revision_sha256", "digest32"),
    ("case_id", "string"),
    ("ordinal", "u32"),
    ("attempt_key", "digest32"),
    ("eval_slot", "u32"),
    ("seed", "u64"),
    ("case_spec_sha256", "digest32"),
    ("model_id", "string"),
    ("model_sha256", "digest32"),
    ("adapter_set_sha256", "digest32"),
    ("template_sha256", "digest32"),
    ("system_messages_sha256", "digest32"),
    ("grammar_sha256", "digest32"),
    ("sampler_sha256", "digest32"),
    ("generation_control_sha256", "digest32"),
    ("original_messages_sha256", "digest32"),
    ("rendered_prompt_sha256", "digest32"),
    ("output_sha256", "digest32"),
    ("prompt_tokens", "u64"),
    ("completion_tokens", "u64"),
    ("finish_reason", "string"),
)
_CLAIM_KEYS = frozenset(name for name, _kind in _FIELDS)
_CORRELATION_FIELDS = frozenset({
    "installation_id",
    "engine_instance_id",
    "principal_id",
    "request_id",
    "trace_id",
    "run_id",
    "suite_id",
    "case_id",
})


class EngineReceiptVerificationError(EngineProtocolError):
    """A terminal engine receipt failed strict verification."""


@dataclass(frozen=True, slots=True)
class EngineReceiptSigner:
    """Receipt signer identity that must match the protected anchor."""

    provider: str
    trust: str
    public_key_spki_der: bytes


@dataclass(frozen=True, slots=True)
class EngineEvalReceipt:
    """Strictly parsed schema-v1 engine evaluation receipt."""

    claims: Mapping[str, int | str]
    receipt_id: str
    signature: bytes
    signer: EngineReceiptSigner

    def __repr__(self) -> str:
        return f"EngineEvalReceipt(receipt_id={self.receipt_id!r}, signer={self.signer.provider!r})"


@dataclass(slots=True)
class EngineReceiptCorrelationTracker:
    """Thread-safe run-scoped duplicate and replay identity tracker."""

    request_ids: set[str] = field(default_factory=set)
    receipt_ids: set[str] = field(default_factory=set)
    attempt_keys: set[str] = field(default_factory=set)
    correlations: set[tuple[str, str, str, str, int]] = field(default_factory=set)
    _lock: Lock = field(default_factory=Lock, repr=False)

    def __repr__(self) -> str:
        return (
            f"EngineReceiptCorrelationTracker(receipts={len(self.receipt_ids)}, correlations={len(self.correlations)})"
        )

    def record(self, receipt: EngineEvalReceipt) -> None:
        """Record a verified receipt, rejecting replay and ambiguity.

        Args:
            receipt: Fully verified receipt to record.

        Raises:
            EngineReceiptVerificationError: If a receipt, request, attempt, or
                case correlation identity was already observed.
        """
        claims = receipt.claims
        request_id = str(claims["request_id"])
        attempt_key = str(claims["attempt_key"])
        correlation = (
            str(claims["installation_id"]),
            str(claims["run_id"]),
            str(claims["suite_id"]),
            str(claims["case_id"]),
            int(claims["ordinal"]),
        )
        with self._lock:
            if receipt.receipt_id in self.receipt_ids:
                raise EngineReceiptVerificationError("engine receipt replayed an existing receipt_id")
            if request_id in self.request_ids:
                raise EngineReceiptVerificationError("engine receipt replayed an existing request_id")
            if attempt_key in self.attempt_keys:
                raise EngineReceiptVerificationError("engine receipt replayed an existing attempt_key")
            if correlation in self.correlations:
                raise EngineReceiptVerificationError("engine receipt creates an ambiguous duplicate case correlation")
            self.receipt_ids.add(receipt.receipt_id)
            self.request_ids.add(request_id)
            self.attempt_keys.add(attempt_key)
            self.correlations.add(correlation)


@dataclass(frozen=True, slots=True)
class EngineReceiptExpectation:
    """Caller-owned values that a signed terminal receipt must match exactly."""

    installation_id: str
    engine_instance_id: str
    principal_id: str
    request_id: str
    trace_id: str
    endpoint: str
    run_id: str
    suite_id: str
    suite_revision_sha256: str
    case_id: str
    ordinal: int
    eval_slot: int
    seed: int
    case_spec_sha256: str
    model_id: str
    output_sha256: str
    original_messages_sha256: str
    model_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.principal_id != "local-supervisor":
            raise ValueError("engine receipt expected principal_id must be local-supervisor")
        if self.endpoint != "/v1/chat/completions":
            raise ValueError("engine receipt expected endpoint must be /v1/chat/completions")
        for field_name, value in (
            ("engine_instance_id", self.engine_instance_id),
            ("principal_id", self.principal_id),
            ("request_id", self.request_id),
            ("trace_id", self.trace_id),
        ):
            if not _IDENTIFIER_RE.fullmatch(value):
                raise ValueError(f"engine receipt expected {field_name} violates the ASCII identifier grammar")

    def __repr__(self) -> str:
        return (
            f"EngineReceiptExpectation(run_id={self.run_id!r}, suite_id={self.suite_id!r}, "
            f"case_id={self.case_id!r}, ordinal={self.ordinal!r}, request_id={self.request_id!r}, "
            f"trace_id={self.trace_id!r})"
        )


@dataclass(frozen=True, slots=True)
class VerifiedEngineReceipt:
    """Authenticated receipt with canonical bytes and trust eligibility."""

    receipt: EngineEvalReceipt
    canonical_bytes: bytes
    production_eligible: bool


def parse_engine_receipt(payload: Mapping[str, Any]) -> EngineEvalReceipt:
    """Parse the exact engine receipt wire object.

    Args:
        payload: Decoded Rust response receipt.

    Returns:
        A typed schema-v1 receipt.

    Raises:
        EngineReceiptVerificationError: If its schema or encodings drift.
    """
    if set(payload) != _TOP_KEYS:
        raise EngineReceiptVerificationError("engine receipt top-level fields do not match schema version 1")
    if payload["canonicalization"] != _CANONICALIZATION or payload["algorithm"] != _ALGORITHM:
        raise EngineReceiptVerificationError("engine receipt canonicalization or algorithm is unsupported")
    raw_claims = payload["claims"]
    if not isinstance(raw_claims, Mapping) or set(raw_claims) != _CLAIM_KEYS:
        raise EngineReceiptVerificationError("engine receipt claims do not match the canonical field contract")
    claims = {name: _parse_claim(name, kind, raw_claims[name]) for name, kind in _FIELDS}
    if claims["schema_version"] != 1:
        raise EngineReceiptVerificationError("engine receipt claims schema_version must be 1")
    raw_signer = payload["signer"]
    if not isinstance(raw_signer, Mapping) or set(raw_signer) != _SIGNER_KEYS:
        raise EngineReceiptVerificationError("engine receipt signer fields do not match schema version 1")
    signer = EngineReceiptSigner(
        provider=_text("signer.provider", raw_signer["provider"]),
        trust=_text("signer.trust", raw_signer["trust"]),
        public_key_spki_der=_base64url("signer.public_key_spki_der", raw_signer["public_key_spki_der"]),
    )
    return EngineEvalReceipt(
        claims=claims,
        receipt_id=_digest("receipt_id", payload["receipt_id"]),
        signature=_base64url("signature", payload["signature"]),
        signer=signer,
    )


def canonicalize_engine_receipt_claims(claims: Mapping[str, Any]) -> bytes:
    """Encode exact ordered ADR-0174 claims as canonical tagged TLV.

    Args:
        claims: Mapping containing exactly the 38 schema-v1 fields.

    Returns:
        Domain-separated canonical receipt bytes.

    Raises:
        EngineReceiptVerificationError: If the claim contract is malformed.
    """
    if set(claims) != _CLAIM_KEYS:
        raise EngineReceiptVerificationError("engine receipt claims do not match the canonical field contract")
    output: list[bytes] = []
    for tag, (name, kind) in enumerate(_FIELDS, start=1):
        value = _parse_claim(name, kind, claims[name])
        output.append(_tlv(tag, _encode_claim(kind, value)))
    return _RECEIPT_DOMAIN + b"".join(output)


def verify_engine_receipt(
    receipt: Mapping[str, Any] | EngineEvalReceipt,
    *,
    anchor: EngineTrustAnchor,
    expected: EngineReceiptExpectation,
    tracker: EngineReceiptCorrelationTracker,
    require_production_anchor: bool = True,
) -> VerifiedEngineReceipt:
    """Verify one receipt against an external anchor and exact request.

    Args:
        receipt: Rust receipt mapping or previously parsed receipt.
        anchor: Independently pinned protected installation anchor.
        expected: Caller-owned evaluation and output bindings.
        tracker: Run-scoped replay and ambiguity guard.
        require_production_anchor: Reject software-test keys for AM_ENGINE.

    Returns:
        Verified receipt, canonical bytes, and eligibility classification.

    Raises:
        EngineReceiptVerificationError: On schema, binding, signature, replay,
            downgrade, orphan, or ambiguity failure.
    """
    parsed = receipt if isinstance(receipt, EngineEvalReceipt) else parse_engine_receipt(receipt)
    if require_production_anchor and not anchor.is_production_eligible:
        raise EngineReceiptVerificationError("software-test receipt signers cannot produce AM_ENGINE evidence")
    _verify_anchor(parsed, anchor)
    _verify_expected(parsed.claims, expected)
    if parsed.claims["attempt_key"] != _attempt_key(parsed.claims):
        raise EngineReceiptVerificationError("engine receipt attempt_key does not match its canonical correlation")
    canonical = canonicalize_engine_receipt_claims(parsed.claims)
    if sha256(canonical).hexdigest() != parsed.receipt_id:
        raise EngineReceiptVerificationError("engine receipt_id does not match the canonical claims")
    _verify_signature(anchor.public_key_spki_der, parsed.signature, canonical)
    tracker.record(parsed)
    return VerifiedEngineReceipt(parsed, canonical, anchor.is_production_eligible)


def verify_persisted_engine_receipt(
    receipt: Mapping[str, Any] | EngineEvalReceipt,
    *,
    trust_context: tuple[Path, str, str],
    expected: EngineReceiptExpectation,
    tracker: EngineReceiptCorrelationTracker,
    require_production_anchor: bool = True,
    allow_untrusted_test_signer: bool = False,
) -> VerifiedEngineReceipt:
    """Reverify a persisted receipt against current or retained anchor state.

    Args:
        receipt: Persisted schema-v1 Rust receipt mapping or parsed receipt.
        trust_context: Supervisor-owned current anchor path, exact current
            anchor digest, and independently pinned P155 authority SPKI digest.
        expected: Persisted caller-owned evaluation and output bindings.
        tracker: Store or run-scoped replay and ambiguity guard.
        require_production_anchor: Reject software-test anchors for AM_ENGINE.
        allow_untrusted_test_signer: Permit software-test anchor loading for
            contract tests without upgrading its eligibility.

    Returns:
        Receipt verified by the exact current or predecessor anchor it names.

    Raises:
        EngineReceiptVerificationError: If the receipt schema is invalid.
        EngineTrustAnchorError: If anchor resolution or its predecessor chain
            fails closed validation.
    """
    parsed = receipt if isinstance(receipt, EngineEvalReceipt) else parse_engine_receipt(receipt)
    anchor = resolve_engine_trust_anchor(
        trust_context,
        requested_anchor_sha256=str(parsed.claims["anchor_sha256"]),
        allow_untrusted_test_signer=allow_untrusted_test_signer,
    )
    return verify_engine_receipt(
        parsed,
        anchor=anchor,
        expected=expected,
        tracker=tracker,
        require_production_anchor=require_production_anchor,
    )


def _verify_anchor(receipt: EngineEvalReceipt, anchor: EngineTrustAnchor) -> None:
    claims = receipt.claims
    pairs = (
        ("installation_id", anchor.installation_id),
        ("anchor_sha256", anchor.anchor_sha256),
        ("key_id", anchor.key_id),
        ("key_epoch", anchor.key_epoch),
        ("engine_release", anchor.engine_release),
        ("source_commit", anchor.source_commit),
        ("libllama_revision", anchor.libllama_revision),
        ("release_manifest_sha256", anchor.release_manifest_sha256),
        ("engine_binary_sha256", anchor.engine_binary_sha256),
    )
    for name, expected_value in pairs:
        if claims[name] != expected_value:
            raise EngineReceiptVerificationError(f"engine receipt {name} does not match the protected trust anchor")
    signer = receipt.signer
    if (
        anchor.algorithm != _ALGORITHM
        or signer.provider != anchor.provider
        or signer.trust != anchor.trust
        or signer.public_key_spki_der != anchor.public_key_spki_der
        or sha256(signer.public_key_spki_der).hexdigest() != claims["key_id"]
    ):
        raise EngineReceiptVerificationError("engine receipt signer identity does not match the protected trust anchor")


def _verify_expected(claims: Mapping[str, int | str], expected: EngineReceiptExpectation) -> None:
    pairs: tuple[tuple[str, int | str], ...] = (
        ("installation_id", expected.installation_id),
        ("engine_instance_id", expected.engine_instance_id),
        ("principal_id", expected.principal_id),
        ("request_id", expected.request_id),
        ("trace_id", expected.trace_id),
        ("endpoint", expected.endpoint),
        ("run_id", expected.run_id),
        ("suite_id", expected.suite_id),
        ("suite_revision_sha256", expected.suite_revision_sha256),
        ("case_id", expected.case_id),
        ("ordinal", expected.ordinal),
        ("eval_slot", expected.eval_slot),
        ("seed", expected.seed),
        ("case_spec_sha256", expected.case_spec_sha256),
        ("model_id", expected.model_id),
        ("output_sha256", expected.output_sha256),
        ("original_messages_sha256", expected.original_messages_sha256),
    )
    for name, expected_value in pairs:
        if claims[name] != expected_value:
            raise EngineReceiptVerificationError(f"engine receipt {name} does not match the expected evaluation")
    if expected.model_sha256 is not None and claims["model_sha256"] != expected.model_sha256:
        raise EngineReceiptVerificationError("engine receipt model_sha256 does not match the expected model artifact")


def _attempt_key(claims: Mapping[str, int | str]) -> str:
    values = (
        str(claims["installation_id"]).encode(),
        str(claims["run_id"]).encode(),
        str(claims["suite_id"]).encode(),
        str(claims["case_id"]).encode(),
        struct.pack(">I", int(claims["ordinal"])),
    )
    stream = _ATTEMPT_DOMAIN + b"".join(starmap(_tlv, enumerate(values, start=1)))
    return sha256(stream).hexdigest()


def _parse_claim(name: str, kind: str, value: Any) -> int | str:
    if kind == "digest32":
        return _digest(name, value)
    if kind == "string":
        result = _text(name, value)
        if name in _CORRELATION_FIELDS and not _IDENTIFIER_RE.fullmatch(result):
            raise EngineReceiptVerificationError(f"engine receipt {name} violates the ASCII identifier grammar")
        return result
    bits = 16 if kind == "u16" else 32 if kind == "u32" else 64
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < 1 << bits:
        raise EngineReceiptVerificationError(f"engine receipt {name} must be an unsigned {bits}-bit integer")
    return value


def _encode_claim(kind: str, value: int | str) -> bytes:
    if kind == "digest32":
        return bytes.fromhex(str(value))
    if kind == "string":
        return str(value).encode("utf-8")
    if kind == "u16":
        return struct.pack(">H", int(value))
    if kind == "u32":
        return struct.pack(">I", int(value))
    return struct.pack(">Q", int(value))


def _verify_signature(spki_der: bytes, signature: bytes, message: bytes) -> None:
    if len(signature) != 64:
        raise EngineReceiptVerificationError("engine receipt signature must be exactly 64-byte P1363")
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:], "big")
    if not 1 <= r < _P256_ORDER or not 1 <= s <= _P256_HALF_ORDER:
        raise EngineReceiptVerificationError("engine receipt signature must contain valid low-S P-256 scalars")
    try:
        key = load_der_public_key(spki_der)
    except (TypeError, ValueError) as exc:
        raise EngineReceiptVerificationError("engine receipt anchor key is invalid SubjectPublicKeyInfo DER") from exc
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        raise EngineReceiptVerificationError("engine receipt anchor key must be P-256")
    try:
        key.verify(utils.encode_dss_signature(r, s), message, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature as exc:
        raise EngineReceiptVerificationError("engine receipt signature is invalid") from exc


def _tlv(tag: int, value: bytes) -> bytes:
    return struct.pack(">HI", tag, len(value)) + value


def _digest(name: str, value: Any) -> str:
    if not isinstance(value, str) or not _HEX_RE.fullmatch(value):
        raise EngineReceiptVerificationError(f"engine receipt {name} must be a lowercase SHA-256 digest")
    return value


def _text(name: str, value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > 4096
        or any(unicodedata.category(character) == "Cc" for character in value)
    ):
        raise EngineReceiptVerificationError(f"engine receipt {name} must be a bounded non-empty UTF-8 string")
    return value


def _base64url(name: str, value: Any) -> bytes:
    if not isinstance(value, str) or not _BASE64URL_RE.fullmatch(value) or "=" in value:
        raise EngineReceiptVerificationError(f"engine receipt {name} must be unpadded base64url")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, base64.binascii.Error) as exc:
        raise EngineReceiptVerificationError(f"engine receipt {name} is invalid base64url") from exc
    if base64.urlsafe_b64encode(decoded).rstrip(b"=").decode() != value:
        raise EngineReceiptVerificationError(f"engine receipt {name} is not canonical base64url")
    return decoded


__all__ = [
    "EngineEvalReceipt",
    "EngineReceiptCorrelationTracker",
    "EngineReceiptExpectation",
    "EngineReceiptSigner",
    "EngineReceiptVerificationError",
    "VerifiedEngineReceipt",
    "canonicalize_engine_receipt_claims",
    "parse_engine_receipt",
    "verify_engine_receipt",
    "verify_persisted_engine_receipt",
]
