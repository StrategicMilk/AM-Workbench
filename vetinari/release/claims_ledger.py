"""Persistent JSONL ledger for Vetinari release claims.

Each ``ReleaseClaimRecord`` appended here is written atomically to
``outputs/release/<version>/ledger.jsonl`` via a tempfile-then-rename strategy
so a mid-write crash never leaves a half-written record.

``ClaimsLedger.verify_all`` walks the ledger and confirms every evidence
artifact still exists on disk and matches its embedded SHA-256 checksum.
Any missing path or checksum mismatch causes a ``LedgerVerificationReport``
with ``passed=False`` (fail-closed, Rule 2).

This is part of the release pipeline: ``release_doctor.py`` builds the wheel
and smoke evidence; ``ClaimsLedger`` persists those claims for later audit by
``scripts/pre_release_gate.py``.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import logging
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from vetinari.boundary_guards import require_nonempty
from vetinari.release.proof_schema import ClaimKind, ReleaseClaimRecord, ReleaseProof, validate_model_license_fields
from vetinari.utils import privacy_receipt

logger = logging.getLogger(__name__)
_SAFE_RELEASE_VERSION = re.compile(r"^[A-Za-z0-9._-]+$")


# --- LedgerVerificationReport ------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class LedgerVerificationReport:
    """Result of walking a ledger file and verifying all evidence artifacts.

    Fail-closed: ``passed`` is ``True`` only when every record's
    ``evidence_path`` exists on disk **and** its SHA-256 matches the
    embedded checksum.  Any deviation sets ``passed=False``.

    Args:
        passed: ``True`` iff all claims resolved without error.
        total: Total number of records examined.
        ok: Number of records that resolved cleanly.
        failures: Human-readable descriptions of each failed record,
            ordered by occurrence in the ledger.

    Returns:
        Immutable verification report.
    """

    passed: bool
    total: int
    ok: int
    failures: tuple[str, ...]

    def __repr__(self) -> str:
        """Return a compact repr showing pass/fail and counts."""
        return (
            f"LedgerVerificationReport(passed={self.passed}, ok={self.ok}/{self.total}, failures={len(self.failures)})"
        )


# --- ClaimsLedger ------------------------------------------------------------


class ClaimsLedger:
    """Append-only JSONL ledger for release claims.

    Each call to ``append()`` serialises one ``ReleaseClaimRecord`` and
    writes it atomically to ``outputs/release/<version>/ledger.jsonl``.
    The file is created (with all parent directories) if it does not yet
    exist.

    The ledger is intentionally append-only: records are never deleted or
    mutated after writing.  ``verify_all()`` is a read-only audit pass.

    Args:
        version: Release version string used as the sub-directory name
            under ``outputs/release/``, e.g. ``"0.7.0"`` or ``"dev"``.
        repo_root: Absolute path to the repository root.  Defaults to the
            parent of ``vetinari/`` (i.e., resolved from this file's
            location).  Evidence paths stored in claim records are
            resolved relative to this root.
    """

    def __init__(
        self,
        version: str,
        repo_root: Path | None = None,
    ) -> None:
        """Initialise the ledger for the given version.

        Args:
            version: Release version string; used as the directory name
                under ``outputs/release/``.
            repo_root: Repository root path.  If ``None``, resolved as
                three parents above this source file (i.e.
                ``vetinari/release/claims_ledger.py`` -> repo root).
        """
        if not version or not version.strip():
            raise ValueError("version must be a non-empty string")
        clean_version = version.strip()
        if _SAFE_RELEASE_VERSION.fullmatch(clean_version) is None or clean_version in {".", ".."}:
            raise ValueError("version must be a safe release directory name")
        self._version = clean_version
        resolved_repo_root = repo_root if repo_root is not None else Path(__file__).resolve().parents[2]
        require_nonempty(str(resolved_repo_root), field_name="repo_root")
        self._repo_root = resolved_repo_root
        self._ledger_path: Path = self._repo_root / "outputs" / "release" / self._version / "ledger.jsonl"

    @property
    def ledger_path(self) -> Path:
        """The absolute path to the JSONL ledger file for this version.

        Returns:
            ``Path`` object pointing to ``outputs/release/<version>/ledger.jsonl``.
        """
        return self._ledger_path

    def append(self, claim: ReleaseClaimRecord) -> None:
        """Append one claim record to the ledger, atomically.

        Serialises *claim* to a single JSON line and writes it to the
        ledger file via ``tempfile.NamedTemporaryFile`` + ``os.replace``
        so a mid-write crash never leaves a corrupt record.

        The strategy is:
        1. Read the existing ledger content (if any).
        2. Write existing content + new line to a sibling tempfile.
        3. ``os.replace()`` the tempfile over the ledger path.

        This means the ledger file is always in a consistent state at the
        filesystem level; readers see either the old file or the fully
        written new file, never a partial state.

        Args:
            claim: The ``ReleaseClaimRecord`` to persist.

        Raises:
            TypeError: If *claim* is not a ``ReleaseClaimRecord``.
            OSError: If the ledger directory cannot be created or written.
        """
        if not isinstance(claim, ReleaseClaimRecord):
            raise TypeError(f"append() expects a ReleaseClaimRecord, got {type(claim).__name__!r}")

        self._ledger_path.parent.mkdir(parents=True, exist_ok=True)

        new_line = _claim_to_jsonl(claim, repo_root=self._repo_root)

        # Read current content first (empty string if file doesn't exist yet).
        existing: str = ""
        if self._ledger_path.exists():
            existing = self._ledger_path.read_text(encoding="utf-8")

        # Build new content: preserve existing lines, append new one.
        combined = existing + new_line + "\n"

        # Atomic write: write to sibling temp, then rename over the target.
        dir_path = self._ledger_path.parent
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=dir_path,
            prefix=".ledger_tmp_",
            suffix=".jsonl",
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                fh.write(combined)
            os.replace(tmp_path, self._ledger_path)
        except Exception:
            # Best-effort cleanup of the orphaned tempfile before re-raising.
            # contextlib.suppress avoids a nested try/except that would
            # trigger VET022/VET023 on the inner except block.
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise

        logger.debug(
            "Appended claim %r to ledger %s",
            claim.id,
            self._ledger_path,
        )

    @staticmethod
    def verify_all(
        ledger_path: Path,
        repo_root: Path | None = None,
    ) -> LedgerVerificationReport:
        """Walk a ledger file and verify every evidence artifact.

        For each record in *ledger_path*:
        - If ``evidence_path`` is non-empty, the file must exist on disk.
        - If the record has a ``sha256`` field (optional extension), the
          file's digest must match.

        The report is fail-closed (Rule 2): ``passed=True`` is returned
        **only** when every record in the ledger resolves cleanly.
        A missing ledger file itself triggers ``passed=False``.

        Args:
            ledger_path: Absolute path to the ``ledger.jsonl`` file to
                audit.
            repo_root: Root path against which relative ``evidence_path``
                values are resolved.  If ``None``, the parent of
                *ledger_path* is used as a safe fallback.

        Returns:
            ``LedgerVerificationReport`` with ``passed=True`` only when
            all checks succeed.
        """
        if repo_root is None:
            repo_root = Path(__file__).resolve().parents[2]
        require_nonempty(str(repo_root), field_name="repo_root")

        if not ledger_path.exists():
            return LedgerVerificationReport(
                passed=False,
                total=0,
                ok=0,
                failures=(f"Ledger file not found: {ledger_path}",),
            )

        content = ledger_path.read_text(encoding="utf-8")
        lines = [ln for ln in content.splitlines() if ln.strip()]

        if not lines:
            return LedgerVerificationReport(
                passed=False,
                total=0,
                ok=0,
                failures=(f"Ledger file contains no certified claims: {ledger_path}",),
            )

        failures, ok_count = _verify_ledger_lines(lines, ledger_path, repo_root)

        total = len(lines)
        passed = len(failures) == 0

        if failures:
            logger.warning(
                "Ledger verification failed: %d/%d claims have issues in %s",
                len(failures),
                total,
                ledger_path,
            )

        return LedgerVerificationReport(
            passed=passed,
            total=total,
            ok=ok_count,
            failures=tuple(failures),
        )


# --- helpers -----------------------------------------------------------------


def _verify_ledger_lines(lines: list[str], ledger_path: Path, repo_root: Path) -> tuple[list[str], int]:
    failures: list[str] = []
    ok_count = 0
    for lineno, raw in enumerate(lines, start=1):
        record, failure = _parse_ledger_record(raw, lineno, ledger_path)
        if failure:
            failures.append(failure)
            continue
        line_failure = _verify_record_evidence(record, lineno, repo_root, ledger_path)
        if line_failure:
            failures.append(line_failure)
            continue
        ok_count += 1
    return failures, ok_count


def _parse_ledger_record(raw: str, lineno: int, ledger_path: Path) -> tuple[dict[str, Any], str]:
    try:
        return json.loads(raw), ""
    except json.JSONDecodeError as exc:
        logger.warning("Ledger line %d in %s is not valid JSON; skipping: %s", lineno, ledger_path, exc)
        return {}, f"Line {lineno}: invalid JSON; {exc}"


def _verify_record_evidence(record: dict[str, Any], lineno: int, repo_root: Path, ledger_path: Path) -> str:
    claim_id = record.get("id", f"<line-{lineno}>")
    for field in ("id", "text", "kind", "verified_at"):
        if not isinstance(record.get(field), str) or not str(record[field]).strip():
            return f"Claim {claim_id!r}: required provenance field {field!r} is missing"
    if record["kind"] not in {kind.value for kind in ClaimKind}:
        return f"Claim {claim_id!r}: unsupported evidence kind {record['kind']!r}"
    try:
        verified_at = datetime.fromisoformat(str(record["verified_at"]).replace("Z", "+00:00"))
    except ValueError:
        logger.warning(
            "Release ledger line %d has invalid verified_at metadata; rejecting the claim record",
            lineno,
        )
        return f"Claim {claim_id!r}: verified_at is not a valid ISO-8601 timestamp"
    if verified_at.tzinfo is None:
        return f"Claim {claim_id!r}: verified_at must include a timezone"
    privacy = record.get("privacy_receipt")
    if not isinstance(privacy, dict) or privacy.get("schema_version") != "vetinari-privacy-envelope.v1":
        return f"Claim {claim_id!r}: release provenance privacy receipt is missing or invalid"

    evidence_rel = record.get("evidence_path", "")
    if not evidence_rel:
        return f"Claim {claim_id!r}: evidence_path is required for a certified release claim"
    evidence_abs = Path(evidence_rel)
    if not evidence_abs.is_absolute():
        evidence_abs = repo_root / evidence_rel
    try:
        evidence_abs.resolve().relative_to(repo_root.resolve())
    except ValueError:
        logger.warning(
            "Release ledger line %d references evidence outside the repository; rejecting the claim record",
            lineno,
        )
        return f"Claim {claim_id!r}: evidence path escapes the repository: {evidence_rel}"
    if evidence_abs.resolve() == ledger_path.resolve():
        return f"Claim {claim_id!r}: ledger cannot certify itself as evidence"
    if not evidence_abs.exists():
        return f"Claim {claim_id!r}: evidence file not found: {evidence_rel}"
    stored_sha = record.get("sha256", "")
    if not stored_sha:
        return f"Claim {claim_id!r}: evidence checksum missing for {evidence_rel}"
    actual_sha = _sha256_file(evidence_abs)
    if actual_sha != stored_sha.lower():
        return (
            f"Claim {claim_id!r}: SHA-256 mismatch for {evidence_rel} "
            f"(stored={stored_sha[:12]}..., actual={actual_sha[:12]}...)"
        )
    if _evidence_self_certifies_claim(evidence_abs, claim_id, repo_root):
        return f"Claim {claim_id!r}: evidence artifact self-references the claim instead of independent proof"
    proof_failure = _validate_release_proof_evidence(evidence_abs, claim_id, repo_root, record)
    if proof_failure:
        return f"Claim {claim_id!r}: {proof_failure}"
    return ""


def _evidence_self_certifies_claim(evidence_path: Path, claim_id: object, repo_root: Path) -> bool:
    """Detect JSON evidence that cites itself as the proof for the same claim."""
    if evidence_path.suffix.lower() != ".json":
        return False
    try:
        payload = json.loads(evidence_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "Release evidence self-reference scan was inconclusive (error_type=%s); "
            "downstream proof validation remains required",
            type(exc).__name__,
        )
        return False

    pending: list[object] = [payload]
    while pending:
        value = pending.pop()
        if isinstance(value, list):
            pending.extend(value)
            continue
        if not isinstance(value, dict):
            continue
        nested_id = value.get("id") or value.get("claim_id")
        nested_evidence = value.get("evidence_path")
        if str(nested_id) == str(claim_id) and isinstance(nested_evidence, str):
            nested_path = Path(nested_evidence)
            if not nested_path.is_absolute():
                nested_path = repo_root / nested_path
            if nested_path.resolve() == evidence_path.resolve():
                return True
        pending.extend(value.values())
    return False


def _validate_release_proof_evidence(
    evidence_path: Path,
    claim_id: object,
    repo_root: Path,
    ledger_record: dict[str, Any],
) -> str:
    """Require authoritative release-doctor schema and independent tool output."""
    if evidence_path.name != "proof.json":
        return "evidence must be a release-doctor proof.json artifact"
    try:
        proof = ReleaseProof.from_json(evidence_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        logger.warning(
            "Release proof schema validation failed (error_type=%s); rejecting the evidence",
            type(exc).__name__,
        )
        return f"evidence is not a valid release-doctor proof: {exc}"
    if proof.version != evidence_path.parent.name:
        return "release proof version does not match its versioned evidence directory"
    if proof.doctor_exit_code != 0 or proof.smoke_exit_code != 0:
        return "release proof doctor and smoke checks must both pass"
    if proof.wheel_size_bytes <= 0 or re.fullmatch(r"[0-9a-fA-F]{64}", proof.wheel_sha256) is None:
        return "release proof must contain a real wheel size and SHA-256"
    matching = [claim for claim in proof.claims if claim.id == str(claim_id)]
    if len(matching) != 1:
        return "release proof must contain exactly one matching claim record"
    source_claim = matching[0]
    if source_claim.kind is not ClaimKind.TOOL_EVIDENCE or not source_claim.evidence_path:
        return "matching release proof claim must cite independent tool evidence"
    tool_evidence = Path(source_claim.evidence_path)
    if not tool_evidence.is_absolute():
        tool_evidence = repo_root / tool_evidence
    try:
        tool_evidence.resolve().relative_to(repo_root.resolve())
    except ValueError:
        logger.warning("Release proof cites independent evidence outside the repository; rejecting the evidence")
        return "matching release proof claim evidence escapes the repository"
    if tool_evidence.resolve() == evidence_path.resolve() or not tool_evidence.is_file():
        return "matching release proof claim must cite an existing independent tool artifact"
    binding = ledger_record.get("independent_evidence")
    if not isinstance(binding, dict):
        return "ledger claim must bind independent tool evidence provenance and SHA-256"
    bound_path_value = binding.get("path")
    bound_sha = binding.get("sha256")
    if not isinstance(bound_path_value, str) or not isinstance(bound_sha, str):
        return "independent tool evidence binding is missing path or SHA-256"
    bound_path = Path(bound_path_value)
    if not bound_path.is_absolute():
        bound_path = repo_root / bound_path
    if bound_path.resolve() != tool_evidence.resolve():
        return "independent tool evidence binding does not match release proof claim provenance"
    if binding.get("kind") != source_claim.kind.value or binding.get("verified_at") != source_claim.verified_at:
        return "independent tool evidence kind or verification timestamp does not match release proof"
    actual_tool_sha = _sha256_file(tool_evidence)
    if re.fullmatch(r"[0-9a-fA-F]{64}", bound_sha) is None or actual_tool_sha != bound_sha.lower():
        return "independent tool evidence SHA-256 mismatch"
    return ""


def _independent_evidence_binding(
    proof_path: Path,
    claim_id: str,
    repo_root: Path,
) -> dict[str, str] | None:
    """Build the transitive digest binding for one release-proof claim."""
    if proof_path.name != "proof.json":
        return None
    try:
        proof = ReleaseProof.from_json(proof_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        logger.warning(
            "Unable to construct an independent evidence binding (error_type=%s); "
            "omitting the binding so verification fails closed",
            type(exc).__name__,
        )
        return None
    matching = [candidate for candidate in proof.claims if candidate.id == claim_id]
    if len(matching) != 1:
        return None
    source_claim = matching[0]
    if source_claim.kind is not ClaimKind.TOOL_EVIDENCE or not source_claim.evidence_path:
        return None
    tool_evidence = Path(source_claim.evidence_path)
    if not tool_evidence.is_absolute():
        tool_evidence = repo_root / tool_evidence
    try:
        relative = tool_evidence.resolve().relative_to(repo_root.resolve())
    except ValueError:
        logger.warning(
            "Independent evidence binding path resolves outside the repository; "
            "omitting the binding so verification fails closed"
        )
        return None
    if tool_evidence.resolve() == proof_path.resolve() or not tool_evidence.is_file():
        return None
    return {
        "path": relative.as_posix(),
        "sha256": _sha256_file(tool_evidence),
        "kind": source_claim.kind.value,
        "verified_at": source_claim.verified_at,
    }


def _claim_to_jsonl(claim: ReleaseClaimRecord, repo_root: Path | None = None) -> str:
    """Serialise a ``ReleaseClaimRecord`` to a single-line JSON string.

    The ``kind`` field is written as its string value so the JSONL is
    self-contained without requiring the enum import at read time.

    Args:
        claim: The claim to serialise.
        repo_root: Repository root used to resolve relative evidence paths
            before computing their digest.

    Returns:
        A single-line JSON string (no trailing newline).
    """
    model_license_failures = validate_model_license_fields(claim)
    if model_license_failures:
        missing = ", ".join(model_license_failures)
        raise ValueError(f"release claim {claim.id!r} is missing required model license fields: {missing}")

    raw: dict[str, Any] = {
        "id": claim.id,
        "text": claim.text,
        "evidence_path": claim.evidence_path,
        "kind": claim.kind.value if isinstance(claim.kind, ClaimKind) else str(claim.kind),
        "verified_at": claim.verified_at,
        "privacy_receipt": privacy_receipt(
            privacy_class="operational",
            retention_days=365,
            source="release.claims_ledger",
            redaction_applied=True,
        ),
    }
    for field in (
        "model_id",
        "model_family",
        "model_license",
        "model_license_url",
        "model_license_notice",
    ):
        value = getattr(claim, field)
        if value:
            raw[field] = value
    if claim.evidence_path:
        evidence_path = Path(claim.evidence_path)
        if not evidence_path.is_absolute() and repo_root is not None:
            evidence_path = repo_root / evidence_path
        if not evidence_path.exists():
            raise FileNotFoundError(f"release claim evidence file not found: {claim.evidence_path}")
        raw["sha256"] = _sha256_file(evidence_path)
        if repo_root is not None:
            binding = _independent_evidence_binding(evidence_path, claim.id, repo_root)
            if binding is not None:
                raw["independent_evidence"] = binding
    return json.dumps(raw, separators=(",", ":"))


def _sha256_file(path: Path) -> str:
    """Compute the lowercase hex SHA-256 digest of a file.

    Args:
        path: Absolute path to the file to hash.

    Returns:
        Lowercase hex-encoded SHA-256 digest string.
    """
    hasher = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()
