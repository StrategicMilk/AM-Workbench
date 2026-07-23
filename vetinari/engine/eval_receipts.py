"""AM Engine evaluation-context and signed-receipt integration.

Python supplies expected evaluation correlation and verifies engine-authored
receipts against an independently pinned installation anchor.  This module has
no receipt-signing surface: Python must never mint ``AM_ENGINE`` evidence.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import vetinari.engine as engine_runtime
from vetinari.engine.client_types import EngineProtocolError, EvalContext
from vetinari.engine.trust_anchor import EngineTrustAnchor, load_engine_trust_anchor
from vetinari.exceptions import EngineUnavailableError

logger = logging.getLogger(__name__)

_ORIGINAL_MESSAGES_DOMAIN = b"AMW\0engine-eval-original-messages-v1\0"
_MESSAGE_COUNT_TAG = 1
_MESSAGE_ROLE_TAG = 2
_MESSAGE_CONTENT_TAG = 3
_ALLOWED_MESSAGE_ROLES = frozenset({"system", "user", "assistant", "tool"})
_ENGINE_EVAL_PRINCIPAL_ID = "local-supervisor"
_ENGINE_EVAL_ENDPOINT = "/v1/chat/completions"


@contextmanager
def _private_ledger_transaction(path: Path) -> Iterator[None]:
    """Lazily enter the established private cross-process JSONL transaction."""
    from vetinari.analytics._private_jsonl import _cost_ledger_transaction

    with _cost_ledger_transaction(path):
        yield


def _required_string(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _required_uint(raw: Mapping[str, Any], key: str, *, bits: int) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < 1 << bits:
        raise ValueError(f"{key} must be an unsigned {bits}-bit integer")
    return value


def _required_sha256(raw: Mapping[str, Any], key: str) -> str:
    value = _required_string(raw, key)
    if len(value) != 64 or value != value.lower() or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{key} must be a 64-character lowercase SHA-256 digest")
    return value


def _tlv(tag: int, value: bytes) -> bytes:
    return tag.to_bytes(2, "big") + len(value).to_bytes(4, "big") + value


def _original_messages_sha256(messages: Sequence[Mapping[str, Any]]) -> str:
    if not messages or len(messages) >= 1 << 32:
        raise ValueError("evaluation messages must be a non-empty u32-bounded sequence")
    payload = bytearray(_ORIGINAL_MESSAGES_DOMAIN)
    payload.extend(_tlv(_MESSAGE_COUNT_TAG, len(messages).to_bytes(4, "big")))
    for message in messages:
        if not isinstance(message, Mapping):
            raise ValueError("evaluation messages must contain objects")
        role = _required_string(message, "role")
        content = _required_string(message, "content")
        if role not in _ALLOWED_MESSAGE_ROLES:
            raise ValueError(f"unsupported evaluation message role {role!r}")
        payload.extend(_tlv(_MESSAGE_ROLE_TAG, role.encode("utf-8")))
        payload.extend(_tlv(_MESSAGE_CONTENT_TAG, content.encode("utf-8")))
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class EvalRequestCorrelation:
    """Runner-authored fields required on every AM Engine EVAL request."""

    run_id: str
    suite_id: str
    suite_revision_sha256: str
    case_id: str
    ordinal: int
    case_spec_sha256: str

    def __repr__(self) -> str:
        """Return the useful correlation identity without verbose digests."""
        return (
            f"{type(self).__name__}(run_id={self.run_id!r}, suite_id={self.suite_id!r}, "
            f"case_id={self.case_id!r}, ordinal={self.ordinal!r})"
        )

    @classmethod
    def from_metadata(cls, metadata: Mapping[str, Any]) -> EvalRequestCorrelation:
        """Decode an exact schema-v1 ``eval_context`` from request metadata.

        Args:
            metadata: Inference request metadata containing ``eval_context``.

        Returns:
            Strict validated run, suite, and case correlation.

        Raises:
            ValueError: If context is absent, malformed, incomplete, or has
                unknown fields.
        """
        raw = metadata.get("eval_context")
        if not isinstance(raw, Mapping):
            raise ValueError("EVAL request metadata requires an eval_context object")
        expected_keys = {
            "schema_version",
            "run_id",
            "suite_id",
            "suite_revision_sha256",
            "case_id",
            "ordinal",
            "case_spec_sha256",
        }
        if set(raw) != expected_keys:
            raise ValueError("eval_context fields do not match schema version 1")
        context = EvalContext(
            schema_version=_required_uint(raw, "schema_version", bits=16),
            run_id=_required_string(raw, "run_id"),
            suite_id=_required_string(raw, "suite_id"),
            suite_revision_sha256=_required_sha256(raw, "suite_revision_sha256"),
            case_id=_required_string(raw, "case_id"),
            ordinal=_required_uint(raw, "ordinal", bits=32),
            case_spec_sha256=_required_sha256(raw, "case_spec_sha256"),
        )
        return cls(
            run_id=context.run_id,
            suite_id=context.suite_id,
            suite_revision_sha256=context.suite_revision_sha256,
            case_id=context.case_id,
            ordinal=context.ordinal,
            case_spec_sha256=context.case_spec_sha256,
        )


@dataclass(frozen=True, slots=True)
class VerifiedEngineResponseReceipt:
    """Minimal authoritative identity extracted after full receipt verification."""

    receipt_id: str
    model_sha256: str


class _EvalReceiptTracker:
    """Lazy wrapper that keeps ordinary adapter construction import-safe."""

    def __init__(self) -> None:
        self.delegate: object | None = None


def create_eval_receipt_tracker() -> _EvalReceiptTracker:
    """Create a run-scoped replay and duplicate-correlation tracker.

    Returns:
        Empty tracker consumed by consecutive engine receipt verifications.
    """
    return _EvalReceiptTracker()


def _verifier_contract() -> tuple[type[Any], type[Any], Any, Any]:
    from vetinari.engine.engine_receipt_verification import (
        EngineReceiptCorrelationTracker,
        EngineReceiptExpectation,
        verify_engine_receipt,
        verify_persisted_engine_receipt,
    )

    return (
        EngineReceiptCorrelationTracker,
        EngineReceiptExpectation,
        verify_engine_receipt,
        verify_persisted_engine_receipt,
    )


def _load_anchor(trust_context: tuple[Path, str, str]) -> EngineTrustAnchor:
    if not isinstance(trust_context, tuple) or len(trust_context) != 3:
        raise ValueError("receipt trust context must contain path, anchor pin, and authority pin")
    anchor_path, anchor_sha256, authority_pin_sha256 = trust_context
    if not isinstance(anchor_path, Path):
        raise ValueError("receipt trust anchor path must be a Path")
    return load_engine_trust_anchor(
        anchor_path,
        expected_anchor_sha256=_required_sha256({"pin": anchor_sha256}, "pin"),
        expected_authority_pin_sha256=_required_sha256({"pin": authority_pin_sha256}, "pin"),
    )


def _verify_with_anchor(
    receipt: Mapping[str, Any],
    *,
    anchor: EngineTrustAnchor,
    correlation: EvalRequestCorrelation,
    engine_instance_id: str,
    request_id: str,
    trace_id: str,
    model_id: str,
    seed: int,
    eval_slot: int,
    messages: Sequence[Mapping[str, Any]],
    output: str,
    tracker: _EvalReceiptTracker,
) -> VerifiedEngineResponseReceipt:
    tracker_type, expectation_type, verify_receipt, _verify_persisted_receipt = _verifier_contract()
    if tracker.delegate is None:
        tracker.delegate = tracker_type()
    claims = receipt.get("claims")
    if not isinstance(claims, Mapping):
        raise ValueError("engine_receipt claims must be an object")
    expected = expectation_type(
        installation_id=anchor.installation_id,
        engine_instance_id=_required_string({"engine_instance_id": engine_instance_id}, "engine_instance_id"),
        principal_id=_ENGINE_EVAL_PRINCIPAL_ID,
        request_id=_required_string({"request_id": request_id}, "request_id"),
        trace_id=_required_string({"trace_id": trace_id}, "trace_id"),
        endpoint=_ENGINE_EVAL_ENDPOINT,
        run_id=correlation.run_id,
        suite_id=correlation.suite_id,
        suite_revision_sha256=correlation.suite_revision_sha256,
        case_id=correlation.case_id,
        ordinal=correlation.ordinal,
        eval_slot=_required_uint({"eval_slot": eval_slot}, "eval_slot", bits=32),
        seed=_required_uint({"seed": seed}, "seed", bits=64),
        case_spec_sha256=correlation.case_spec_sha256,
        model_id=_required_string({"model_id": model_id}, "model_id"),
        output_sha256=hashlib.sha256(_required_string({"output": output}, "output").encode("utf-8")).hexdigest(),
        original_messages_sha256=_original_messages_sha256(messages),
    )
    verify_receipt(
        receipt,
        anchor=anchor,
        expected=expected,
        tracker=tracker.delegate,
        require_production_anchor=True,
    )
    return VerifiedEngineResponseReceipt(
        receipt_id=_required_sha256(receipt, "receipt_id"),
        model_sha256=_required_sha256(claims, "model_sha256"),
    )


def _verify_persisted_with_context(
    receipt: Mapping[str, Any],
    *,
    trust_context: tuple[Path, str, str],
    correlation: EvalRequestCorrelation,
    engine_instance_id: str,
    request_id: str,
    trace_id: str,
    model_id: str,
    seed: int,
    eval_slot: int,
    messages: Sequence[Mapping[str, Any]],
    output: str,
    tracker: _EvalReceiptTracker,
) -> VerifiedEngineResponseReceipt:
    claims = receipt.get("claims")
    if not isinstance(claims, Mapping):
        raise ValueError("engine_receipt claims must be an object")
    tracker_type, expectation_type, _verify_live_receipt, verify_persisted_receipt = _verifier_contract()
    if tracker.delegate is None:
        tracker.delegate = tracker_type()
    anchor_path, expected_anchor_sha256, expected_authority_pin_sha256 = trust_context
    current_anchor = _load_anchor((anchor_path, expected_anchor_sha256, expected_authority_pin_sha256))
    expected = expectation_type(
        installation_id=current_anchor.installation_id,
        engine_instance_id=_required_string({"engine_instance_id": engine_instance_id}, "engine_instance_id"),
        principal_id=_ENGINE_EVAL_PRINCIPAL_ID,
        request_id=_required_string({"request_id": request_id}, "request_id"),
        trace_id=_required_string({"trace_id": trace_id}, "trace_id"),
        endpoint=_ENGINE_EVAL_ENDPOINT,
        run_id=correlation.run_id,
        suite_id=correlation.suite_id,
        suite_revision_sha256=correlation.suite_revision_sha256,
        case_id=correlation.case_id,
        ordinal=correlation.ordinal,
        eval_slot=_required_uint({"eval_slot": eval_slot}, "eval_slot", bits=32),
        seed=_required_uint({"seed": seed}, "seed", bits=64),
        case_spec_sha256=correlation.case_spec_sha256,
        model_id=_required_string({"model_id": model_id}, "model_id"),
        output_sha256=hashlib.sha256(_required_string({"output": output}, "output").encode("utf-8")).hexdigest(),
        original_messages_sha256=_original_messages_sha256(messages),
    )
    verified = verify_persisted_receipt(
        receipt,
        trust_context=(anchor_path, current_anchor.anchor_sha256, expected_authority_pin_sha256),
        expected=expected,
        tracker=tracker.delegate,
        require_production_anchor=True,
    )
    return VerifiedEngineResponseReceipt(
        receipt_id=verified.receipt.receipt_id,
        model_sha256=_required_sha256(verified.receipt.claims, "model_sha256"),
    )


def verify_engine_response_receipt(
    receipt: object,
    *,
    trust_context: tuple[Path, str, str],
    correlation: EvalRequestCorrelation,
    engine_instance_id: str,
    request_id: str,
    trace_id: str,
    model_id: str,
    seed: int,
    eval_slot: int,
    messages: Sequence[Mapping[str, Any]],
    output: str,
    tracker: _EvalReceiptTracker,
) -> VerifiedEngineResponseReceipt:
    """Verify one engine response against the exact request and pinned anchor.

    Args:
        receipt: Engine-returned signed receipt object.
        trust_context: Supervisor-owned anchor path, exact anchor digest, and
            independently configured P155 authority-key digest.
        correlation: Runner-authored suite/case correlation.
        engine_instance_id: Supervisor-verified active engine process identity.
        request_id: Actual engine response request identifier.
        trace_id: Exact client-authored trace identifier returned by the client.
        model_id: Expected logical model identifier.
        seed: Explicit evaluation seed.
        eval_slot: Allocated evaluation slot.
        messages: Exact ordered request messages.
        output: Exact emitted terminal output.
        tracker: Run-scoped replay/correlation tracker.

    Returns:
        Verified receipt and actual signed model artifact identity.

    Raises:
        EngineProtocolError: If anchor, signature, claims, or correlation fail.
        ValueError: If local expected values are malformed.
    """
    if not isinstance(receipt, Mapping):
        raise ValueError("AM Engine EVAL response omitted engine_receipt")
    return _verify_with_anchor(
        receipt,
        anchor=_load_anchor(trust_context),
        correlation=correlation,
        engine_instance_id=engine_instance_id,
        request_id=request_id,
        trace_id=trace_id,
        model_id=model_id,
        seed=seed,
        eval_slot=eval_slot,
        messages=messages,
        output=output,
        tracker=tracker,
    )


def verify_eval_receipt_bindings(bindings: Sequence[Mapping[str, Any]]) -> bool:
    """Reverify every persisted v6 receipt against current pinned trust state.

    Args:
        bindings: Exact run/case/model/input/output expectations plus complete
            engine receipt objects.

    Returns:
        ``True`` only when every receipt verifies, all correlations are unique,
        and every case used the same signed model artifact digest.
    """
    if not bindings:
        return False
    try:
        trust_context = engine_runtime.get_supervisor().receipt_trust_context()
        tracker = create_eval_receipt_tracker()
        model_sha256s: set[str] = set()
        for binding in bindings:
            raw_receipt = binding.get("engine_receipt")
            if not isinstance(raw_receipt, Mapping):
                return False
            correlation = EvalRequestCorrelation(
                run_id=_required_string(binding, "run_id"),
                suite_id=_required_string(binding, "suite_id"),
                suite_revision_sha256=_required_sha256(binding, "suite_revision_sha256"),
                case_id=_required_string(binding, "case_id"),
                ordinal=_required_uint(binding, "ordinal", bits=32),
                case_spec_sha256=_required_sha256(binding, "case_spec_sha256"),
            )
            messages = binding.get("messages")
            if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
                return False
            verified = _verify_persisted_with_context(
                raw_receipt,
                trust_context=trust_context,
                correlation=correlation,
                engine_instance_id=_required_string(binding, "engine_instance_id"),
                request_id=_required_string(binding, "request_id"),
                trace_id=_required_string(binding, "trace_id"),
                model_id=_required_string(binding, "model_id"),
                seed=_required_uint(binding, "seed", bits=64),
                eval_slot=_required_uint(binding, "eval_slot", bits=32),
                messages=messages,
                output=_required_string(binding, "output"),
                tracker=tracker,
            )
            if verified.receipt_id != _required_sha256(binding, "receipt_id"):
                return False
            expected_model_sha256 = _required_sha256(binding, "model_sha256")
            if verified.model_sha256 != expected_model_sha256:
                return False
            model_sha256s.add(verified.model_sha256)
        return len(model_sha256s) == 1
    except (EngineProtocolError, EngineUnavailableError, OSError, TypeError, ValueError):
        logger.warning("Evaluation receipt provenance is unavailable or invalid", exc_info=True)
        return False
