"""Fail-closed Workbench tool-output squashing."""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import yaml


class ToolOutputSquasherError(ValueError):
    """Raised when a tool-output squasher request is malformed."""


class OutputSourceKind(str, Enum):
    """Known tool-output origins."""

    TERMINAL = "terminal"
    CI = "ci"
    WATCHER = "watcher"
    EVAL = "eval"
    AUTOMATION = "automation"


class OutputLineClass(str, Enum):
    """Line classification emitted by the deterministic classifier."""

    HAZARD = "hazard"
    OUTCOME = "outcome"
    EVIDENCE = "evidence"
    NOISE = "noise"


class ToolOutputSquasherStatus(str, Enum):
    """Preview safety status."""

    CLEAN = "clean"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class RawOutputRef:
    """Guarded reference to raw or redacted raw output."""

    ref: str
    kind: str = "artifact"
    redacted: bool = True
    guarded: bool = True

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"RawOutputRef(ref={self.ref!r}, kind={self.kind!r}, redacted={self.redacted!r})"


@dataclass(frozen=True, slots=True)
class OutputHazard:
    """Hazard preserved from raw output."""

    kind: str
    text: str
    evidence_ref: str


@dataclass(frozen=True, slots=True)
class OutputOutcome:
    """Outcome line preserved from raw output."""

    kind: str
    text: str
    evidence_ref: str


@dataclass(frozen=True, slots=True)
class OutputLineClassification:
    """Classified and redacted line."""

    line_number: int
    line_class: OutputLineClass
    text: str
    evidence_ref: str

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"OutputLineClassification(line_number={self.line_number!r}, line_class={self.line_class!r}, text={self.text!r})"


@dataclass(frozen=True, slots=True)
class SavingsMetrics:
    """Honest compression and token-savings estimates."""

    raw_bytes: int
    squashed_bytes: int
    estimated_raw_tokens: int
    estimated_squashed_tokens: int
    estimated_token_savings: int
    compression_ratio: float
    hazard_count: int
    outcome_count: int

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"SavingsMetrics(raw_bytes={self.raw_bytes!r}, squashed_bytes={self.squashed_bytes!r}, estimated_raw_tokens={self.estimated_raw_tokens!r})"


@dataclass(frozen=True, slots=True)
class ToolOutputSquasherPolicy:
    """Static squashing policy loaded from config."""

    require_raw_ref_for_hazards: bool = True
    block_compressed_only_hazards: bool = True
    preserve_tracebacks: bool = True
    preserve_file_line_evidence: bool = True
    max_noise_lines: int = 8
    min_bytes_to_claim_savings: int = 256

    @classmethod
    def from_config(cls, path: str | Path) -> ToolOutputSquasherPolicy:
        """Execute the from config operation.

        Returns:
            ToolOutputSquasherPolicy value produced by from_config().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        try:
            loaded = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        except OSError as exc:
            raise ToolOutputSquasherError(f"unable to read squasher config: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ToolOutputSquasherError("squasher config must be a mapping")
        policy = loaded.get("policy", loaded)
        if not isinstance(policy, dict):
            raise ToolOutputSquasherError("squasher policy must be a mapping")
        return cls(
            require_raw_ref_for_hazards=bool(policy.get("require_raw_ref_for_hazards", True)),
            block_compressed_only_hazards=bool(policy.get("block_compressed_only_hazards", True)),
            preserve_tracebacks=bool(policy.get("preserve_tracebacks", True)),
            preserve_file_line_evidence=bool(policy.get("preserve_file_line_evidence", True)),
            max_noise_lines=max(0, int(policy.get("max_noise_lines", 8))),
            min_bytes_to_claim_savings=max(0, int(policy.get("min_bytes_to_claim_savings", 256))),
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ToolOutputSquasherPolicy(require_raw_ref_for_hazards={self.require_raw_ref_for_hazards!r}, block_compressed_only_hazards={self.block_compressed_only_hazards!r}, preserve_tracebacks={self.preserve_tracebacks!r})"


@dataclass(frozen=True, slots=True)
class ToolOutputSquasherRequest:
    """Request for a redacted, non-authoritative output preview."""

    raw_text: str
    source_kind: OutputSourceKind
    command: str = ""
    run_id: str = ""
    exit_code: int | None = None
    artifact_ref: str | None = None
    raw_output_ref: RawOutputRef | None = None
    repro_capsule_ref: str | None = None
    max_preview_lines: int | None = None

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> ToolOutputSquasherRequest:
        """Execute the from mapping operation.

        Returns:
            ToolOutputSquasherRequest value produced by from_mapping().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        if not isinstance(payload, dict):
            raise ToolOutputSquasherError("payload must be an object")
        raw_text = payload.get("raw_text", payload.get("output", ""))
        if not isinstance(raw_text, str):
            raise ToolOutputSquasherError("raw_text must be a string")
        raw_ref_payload = payload.get("raw_output_ref")
        raw_ref = None
        if isinstance(raw_ref_payload, dict):
            raw_ref = RawOutputRef(
                ref=str(raw_ref_payload.get("ref", "")),
                kind=str(raw_ref_payload.get("kind", "artifact")),
                redacted=bool(raw_ref_payload.get("redacted", True)),
                guarded=bool(raw_ref_payload.get("guarded", True)),
            )
        elif isinstance(raw_ref_payload, str) and raw_ref_payload:
            raw_ref = RawOutputRef(ref=raw_ref_payload)
        exit_code = payload.get("exit_code")
        return cls(
            raw_text=raw_text,
            source_kind=OutputSourceKind(str(payload.get("source_kind", OutputSourceKind.TERMINAL.value))),
            command=str(payload.get("command", "")),
            run_id=str(payload.get("run_id", "")),
            exit_code=None if exit_code is None else int(exit_code),
            artifact_ref=_optional_str(payload.get("artifact_ref")),
            raw_output_ref=raw_ref,
            repro_capsule_ref=_optional_str(payload.get("repro_capsule_ref")),
            max_preview_lines=None if payload.get("max_preview_lines") is None else int(payload["max_preview_lines"]),
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ToolOutputSquasherRequest(raw_text={self.raw_text!r}, source_kind={self.source_kind!r}, command={self.command!r})"


@dataclass(frozen=True, slots=True)
class ToolOutputSquasherResult:
    """Redacted preview result plus preservation metadata."""

    status: ToolOutputSquasherStatus
    source_kind: OutputSourceKind
    command: str
    run_id: str
    preview: str
    hazards: tuple[OutputHazard, ...]
    outcomes: tuple[OutputOutcome, ...]
    evidence_refs: tuple[str, ...]
    raw_refs: tuple[RawOutputRef, ...]
    metrics: SavingsMetrics
    reasons: tuple[str, ...] = ()
    line_classes: tuple[OutputLineClassification, ...] = ()
    authoritative: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Execute the to dict operation.

        Returns:
            dict[str, Any] value produced by to_dict().
        """
        payload = _jsonable(asdict(self))
        payload.pop("line_classes", None)
        return payload

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ToolOutputSquasherResult(status={self.status!r}, source_kind={self.source_kind!r}, command={self.command!r})"


class ToolOutputSquasherService:
    """Produce redacted, deterministic previews without mutating shared state."""

    def __init__(self, policy: ToolOutputSquasherPolicy | None = None) -> None:
        self.policy = policy or ToolOutputSquasherPolicy()

    def squash(self, request: ToolOutputSquasherRequest) -> ToolOutputSquasherResult:
        """Execute the squash operation.

        Returns:
            ToolOutputSquasherResult value produced by squash().
        """
        clean_text = _strip_control_sequences(request.raw_text)
        redacted_text, redaction_hazards = _redact_sensitive(clean_text)
        classifications = self._classify_lines(redacted_text, request)
        hazards = tuple(
            OutputHazard("redaction_warning", warning, "redaction-warning") for warning in redaction_hazards
        ) + tuple(
            OutputHazard(_hazard_kind(row.text), row.text, row.evidence_ref)
            for row in classifications
            if row.line_class is OutputLineClass.HAZARD
        )
        outcomes = tuple(
            OutputOutcome(_outcome_kind(row.text), row.text, row.evidence_ref)
            for row in classifications
            if row.line_class is OutputLineClass.OUTCOME
        )
        preview = self._preview(classifications, max_lines=request.max_preview_lines)
        raw_refs = _raw_refs(request)
        status = ToolOutputSquasherStatus.CLEAN
        reasons: list[str] = []
        if hazards:
            status = ToolOutputSquasherStatus.DEGRADED
            reasons.append("hazards_preserved_in_preview")
        if redaction_hazards:
            status = ToolOutputSquasherStatus.DEGRADED
            reasons.append("sensitive_values_redacted")
        if hazards and self.policy.require_raw_ref_for_hazards and not raw_refs:
            status = ToolOutputSquasherStatus.BLOCKED
            reasons.append("hazardous_output_requires_raw_output_or_repro_capsule_ref")
        if request.exit_code not in (None, 0):
            reasons.append(f"non_zero_exit_code:{request.exit_code}")
        return ToolOutputSquasherResult(
            status=status,
            source_kind=request.source_kind,
            command=_redact_text(request.command),
            run_id=_redact_text(request.run_id),
            preview=preview,
            hazards=hazards,
            outcomes=outcomes,
            evidence_refs=tuple(
                row.evidence_ref for row in classifications if row.line_class is not OutputLineClass.NOISE
            ),
            raw_refs=raw_refs,
            metrics=_metrics(request.raw_text, preview, len(hazards), len(outcomes), self.policy),
            reasons=tuple(dict.fromkeys(reasons)),
            line_classes=classifications,
        )

    @staticmethod
    def _classify_lines(
        text: str,
        request: ToolOutputSquasherRequest,
    ) -> tuple[OutputLineClassification, ...]:
        rows: list[OutputLineClassification] = []
        for index, raw_line in enumerate(text.splitlines(), start=1):
            line = raw_line.strip()
            if not line:
                continue
            rows.append(
                OutputLineClassification(
                    line_number=index,
                    line_class=_line_class(line),
                    text=line,
                    evidence_ref=f"line-{index}",
                )
            )
        if request.exit_code not in (None, 0):
            rows.append(
                OutputLineClassification(
                    line_number=len(rows) + 1,
                    line_class=OutputLineClass.HAZARD,
                    text=f"non-zero exit code: {request.exit_code}",
                    evidence_ref="exit-code",
                )
            )
        return tuple(rows)

    def _preview(self, rows: tuple[OutputLineClassification, ...], *, max_lines: int | None) -> str:
        preview: list[str] = []
        seen_noise: dict[str, int] = {}
        noise_emitted = 0
        for row in rows:
            if row.line_class is OutputLineClass.NOISE:
                key = _noise_key(row.text)
                seen_noise[key] = seen_noise.get(key, 0) + 1
                if seen_noise[key] > 1:
                    continue
                if noise_emitted >= self.policy.max_noise_lines:
                    continue
                noise_emitted += 1
            preview.append(row.text)
        for key, count in seen_noise.items():
            if count > 1:
                preview.append(f"[squashed {count - 1} repeated lines matching: {key}]")
        if max_lines is not None and len(preview) > max_lines:
            preview = [*preview[:max_lines], f"[squashed {len(preview) - max_lines} additional preview lines]"]
        return "\n".join(preview)


_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_FILE_LINE_RE = re.compile(r"(^|\s)([A-Za-z]:)?[^:\s]+(?:/|\\)[^:\s]+:\d+(?::\d+)?")
_STACK_RE = re.compile(r'File ".*?", line \d+')
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("api_key", re.compile(r"\b(?:sk-[A-Za-z0-9_-]{12,}|api[_-]?key\s*=\s*[A-Za-z0-9._-]+)", re.IGNORECASE)),
    ("pat", re.compile(r"\b(?:ghp|github_pat|pat)_[A-Za-z0-9_]{12,}\b", re.IGNORECASE)),
    ("bearer", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE)),
    (
        "credential",
        re.compile(
            r"\b(?:password|passwd|secret|token|api[_-]?key|aws_secret_access_key)\s*[:=]\s*[^\s,;]+",
            re.IGNORECASE,
        ),
    ),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL)),
    ("email", re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)),
)


def _strip_control_sequences(text: str) -> str:
    return _CONTROL_RE.sub("", _ANSI_RE.sub("", text)).replace("\r\n", "\n").replace("\r", "\n")


def _redact_sensitive(text: str) -> tuple[str, tuple[str, ...]]:
    warnings: list[str] = []
    redacted = text
    for label, pattern in _SECRET_PATTERNS:
        if pattern.search(redacted):
            warnings.append(f"redacted sensitive {label} value")
            redacted = pattern.sub(f"[REDACTED:{label}]", redacted)
    return redacted, tuple(warnings)


def _redact_text(text: str) -> str:
    return _redact_sensitive(_strip_control_sequences(text))[0]


def _line_class(line: str) -> OutputLineClass:
    lower = line.lower()
    if _STACK_RE.search(line) or _FILE_LINE_RE.search(line):
        return OutputLineClass.EVIDENCE
    if (
        "traceback" in lower
        or "permission denied" in lower
        or "access denied" in lower
        or "security" in lower
        or "critical" in lower
        or "severity: high" in lower
        or "timed out" in lower
        or "timeout" in lower
        or "killed" in lower
        or "rm -rf" in lower
        or "non-zero exit" in lower
        or re.search(r"\b(error|failed|failure|exception)\b", lower)
    ):
        return OutputLineClass.HAZARD
    if re.search(r"\b(passed|failed|collected|verdict|summary|changed files|artifact|success|complete)\b", lower):
        return OutputLineClass.OUTCOME
    return OutputLineClass.NOISE


def _hazard_kind(text: str) -> str:
    lower = text.lower()
    if "permission" in lower or "access denied" in lower:
        return "permission"
    if "security" in lower or "critical" in lower or "severity" in lower:
        return "security"
    if "timeout" in lower or "timed out" in lower:
        return "timeout"
    if "rm -rf" in lower:
        return "dangerous_command"
    if "non-zero exit" in lower:
        return "non_zero_exit"
    if "traceback" in lower or "exception" in lower:
        return "stack_trace"
    return "failure"


def _outcome_kind(text: str) -> str:
    lower = text.lower()
    if "artifact" in lower:
        return "artifact"
    if "changed files" in lower:
        return "changed_files"
    if "verdict" in lower:
        return "verdict"
    return "summary"


def _noise_key(text: str) -> str:
    return re.sub(r"\d+", "<n>", text)[:120]


def _raw_refs(request: ToolOutputSquasherRequest) -> tuple[RawOutputRef, ...]:
    refs: list[RawOutputRef] = []
    if (
        request.raw_output_ref
        and request.raw_output_ref.ref
        and request.raw_output_ref.redacted
        and request.raw_output_ref.guarded
    ):
        refs.append(request.raw_output_ref)
    if request.artifact_ref:
        refs.append(RawOutputRef(ref=request.artifact_ref, kind="artifact", redacted=True, guarded=True))
    if request.repro_capsule_ref:
        refs.append(RawOutputRef(ref=request.repro_capsule_ref, kind="repro_capsule", redacted=True, guarded=True))
    return tuple(refs)


def _metrics(
    raw: str, preview: str, hazard_count: int, outcome_count: int, policy: ToolOutputSquasherPolicy
) -> SavingsMetrics:
    raw_bytes = len(raw.encode("utf-8"))
    squashed_bytes = len(preview.encode("utf-8"))
    raw_tokens = math.ceil(raw_bytes / 4)
    squashed_tokens = math.ceil(squashed_bytes / 4)
    savings = max(0, raw_tokens - squashed_tokens)
    if raw_bytes < policy.min_bytes_to_claim_savings or raw_bytes <= squashed_bytes:
        savings = 0
    return SavingsMetrics(
        raw_bytes=raw_bytes,
        squashed_bytes=squashed_bytes,
        estimated_raw_tokens=raw_tokens,
        estimated_squashed_tokens=squashed_tokens,
        estimated_token_savings=savings,
        compression_ratio=round(squashed_bytes / raw_bytes, 4) if raw_bytes else 0.0,
        hazard_count=hazard_count,
        outcome_count=outcome_count,
    )


def _optional_str(value: Any) -> str | None:
    return value if value is None else str(value)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


__all__ = [
    "OutputHazard",
    "OutputLineClass",
    "OutputLineClassification",
    "OutputOutcome",
    "OutputSourceKind",
    "RawOutputRef",
    "SavingsMetrics",
    "ToolOutputSquasherError",
    "ToolOutputSquasherPolicy",
    "ToolOutputSquasherRequest",
    "ToolOutputSquasherResult",
    "ToolOutputSquasherService",
    "ToolOutputSquasherStatus",
]
