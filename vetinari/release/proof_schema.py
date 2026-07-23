"""Release proof artifact schema for Vetinari.

Defines the frozen dataclasses that represent a single release-doctor run.
Every release claim MUST be backed by a ``ReleaseProof`` JSON artifact written
to ``outputs/release/<version>/proof.json``.

Schema version:
    PROOF_SCHEMA_VERSION = "1"

    Consumers MUST refuse artifacts whose ``schema_version`` field does not
    equal ``PROOF_SCHEMA_VERSION``.  There is intentionally no backwards-compat
    fallback -- old artifacts must be regenerated, not silently accepted.

This is step 0 of the release pipeline: schema definition that all other
release tooling builds on.
"""

from __future__ import annotations

import dataclasses
import json
from enum import Enum
from typing import Any

# Schema version.  Bump when any field is added, removed, or renamed.
# Consumers must refuse artifacts with a different version.
PROOF_SCHEMA_VERSION: str = "1"
LLAMA3_MODEL_MARKERS: tuple[str, ...] = (
    "llama-3",
    "llama3",
    "meta-llama/llama-3",
    "meta llama 3",
)
LLAMA3_LICENSE_NAME_FRAGMENT = "llama 3"
LLAMA3_LICENSE_URL = "https://github.com/meta-llama/llama-models/tree/main/models"


class ClaimKind(str, Enum):
    """Evidence category for a single release claim.

    Using ``str`` mixin so the enum value serialises to a plain string in JSON
    without a custom encoder.
    """

    TOOL_EVIDENCE = "tool_evidence"  # Output from a deterministic tool run
    LLM_JUDGMENT = "llm_judgment"  # Output assessed by a language model
    HUMAN_ATTESTED = "human_attested"  # Manually reviewed and signed off


@dataclasses.dataclass(frozen=True, slots=True)
class ReleaseClaimRecord:
    """A single verifiable claim attached to a release proof.

    Args:
        id: Unique identifier for this claim (e.g. ``"wheel-sha256-match"``).
        text: Human-readable description of what was verified.
        evidence_path: Relative path to the evidence artifact (log file,
            screenshot, test output) from the repo root, or ``""`` if no
            file evidence was produced.
        kind: Category of evidence backing this claim.
        verified_at: ISO-8601 UTC timestamp when the claim was verified.
        model_id: Optional model identifier when the claim certifies a model
            artifact or model-dependent release assertion.
        model_family: Optional model family name, for example
            ``"llama-3.1"``.
        model_license: Optional model license name. Llama 3.x references must
            populate this with the version-specific Community License.
        model_license_url: Optional URL to the authoritative model license.
        model_license_notice: Optional attribution or NOTICE text required by
            the model license.

    Returns:
        Immutable claim record.
    """

    id: str
    text: str
    evidence_path: str
    kind: ClaimKind
    verified_at: str  # ISO-8601 UTC, e.g. "2026-04-24T12:00:00+00:00"
    model_id: str = ""
    model_family: str = ""
    model_license: str = ""
    model_license_url: str = ""
    model_license_notice: str = ""

    def __repr__(self) -> str:
        """Return a compact repr showing id and kind."""
        return f"ReleaseClaimRecord(id={self.id!r}, kind={self.kind.value!r})"


@dataclasses.dataclass(frozen=True, slots=True)
class ReleaseProof:
    """Authoritative evidence record for one release-doctor run.

    Written as JSON to ``outputs/release/<version>/proof.json``.  No release
    claim may be closed without a valid ``ReleaseProof`` artifact.

    Fields are intentionally flat -- no nested dicts -- so the JSON round-trip
    is lossless and the schema can be validated by a simple JSON Schema if
    needed.

    Args:
        schema_version: Must equal ``PROOF_SCHEMA_VERSION``.  Consumers refuse
            artifacts where this field differs.
        version: The Vetinari release version string being proved (e.g. ``"0.9.0"``).
        built_at: ISO-8601 UTC timestamp when the wheel was built.
        python_version: Python interpreter version used for the build venv
            (e.g. ``"3.12.3"``).
        host_os: OS platform string from ``platform.platform()`` on the build host.
        wheel_sha256: Hex-encoded SHA-256 digest of the built ``.whl`` file.
        wheel_size_bytes: Size of the ``.whl`` file in bytes.
        doctor_exit_code: Exit code from ``python -m vetinari --doctor`` in the
            clean-venv; 0 = healthy, non-zero = degraded/failed.
        smoke_exit_code: Exit code from the minimal smoke request; 0 = passed.
        smoke_latency_ms: Round-trip latency of the smoke request in milliseconds.
        signed: Whether a detached ``.sig`` signature file was produced.
        claims: Ordered list of individual claim records proving discrete checks.

    Returns:
        Immutable proof artifact.
    """

    schema_version: str
    version: str
    built_at: str  # ISO-8601 UTC
    python_version: str
    host_os: str
    wheel_sha256: str
    wheel_size_bytes: int
    doctor_exit_code: int
    smoke_exit_code: int
    smoke_latency_ms: float
    signed: bool
    claims: tuple[ReleaseClaimRecord, ...]

    def __repr__(self) -> str:
        """Return a compact repr showing version, exit codes, and claim count."""
        return (
            f"ReleaseProof(version={self.version!r}, "
            f"doctor_exit_code={self.doctor_exit_code}, "
            f"smoke_exit_code={self.smoke_exit_code}, "
            f"claims={len(self.claims)})"
        )

    def to_json(self) -> str:
        """Serialise the proof to a JSON string with 2-space indentation.

        Uses ``dataclasses.asdict()`` for field extraction; ``ClaimKind`` enum
        values serialise to their string values via the ``str`` mixin.

        Returns:
            Pretty-printed JSON string suitable for writing to ``proof.json``.
        """
        raw: dict[str, Any] = dataclasses.asdict(self)
        # dataclasses.asdict() converts nested dataclasses to dicts; ClaimKind
        # is a str-enum so its .value is already a str -- but asdict returns the
        # enum object itself for non-dataclass fields.  Normalise here.
        for claim in raw.get("claims", []):
            if isinstance(claim.get("kind"), ClaimKind):
                claim["kind"] = claim["kind"].value
        return json.dumps(raw, indent=2)

    @classmethod
    def from_json(cls, text: str) -> ReleaseProof:
        """Deserialise a proof from a JSON string, refusing stale schema versions.

        Args:
            text: JSON string previously produced by ``to_json()``.

        Returns:
            A ``ReleaseProof`` instance with all fields populated.

        Raises:
            ValueError: If ``schema_version`` in the JSON does not equal
                ``PROOF_SCHEMA_VERSION``, or if required fields are missing.
        """
        raw: dict[str, Any] = json.loads(text)
        version_in_artifact = raw.get("schema_version", "<missing>")
        if version_in_artifact != PROOF_SCHEMA_VERSION:
            raise ValueError(
                f"Refusing stale proof artifact: schema_version "
                f"{version_in_artifact!r} != expected {PROOF_SCHEMA_VERSION!r}. "
                "Regenerate the proof with the current release_doctor."
            )
        claims = tuple(
            ReleaseClaimRecord(
                id=c["id"],
                text=c["text"],
                evidence_path=c["evidence_path"],
                kind=ClaimKind(c["kind"]),
                verified_at=c["verified_at"],
                model_id=c.get("model_id", ""),
                model_family=c.get("model_family", ""),
                model_license=c.get("model_license", ""),
                model_license_url=c.get("model_license_url", ""),
                model_license_notice=c.get("model_license_notice", ""),
            )
            for c in raw.get("claims", [])
        )
        raw_signed = raw["signed"]
        if not isinstance(raw_signed, bool):
            raise ValueError(f"signed field must be a literal bool, got {type(raw_signed).__name__}")
        return cls(
            schema_version=raw["schema_version"],
            version=raw["version"],
            built_at=raw["built_at"],
            python_version=raw["python_version"],
            host_os=raw["host_os"],
            wheel_sha256=raw["wheel_sha256"],
            wheel_size_bytes=int(raw["wheel_size_bytes"]),
            doctor_exit_code=int(raw["doctor_exit_code"]),
            smoke_exit_code=int(raw["smoke_exit_code"]),
            smoke_latency_ms=float(raw["smoke_latency_ms"]),
            signed=raw_signed is True,
            claims=claims,
        )


def model_claim_requires_llama3_license(claim: ReleaseClaimRecord) -> bool:
    """Return whether *claim* references a Llama 3.x model family.

    Returns:
        True when any claim field references a Llama 3.x marker.
    """
    haystack = " ".join((
        claim.id,
        claim.text,
        claim.model_id,
        claim.model_family,
    )).lower()
    normalised = haystack.replace("_", "-")
    return any(marker in normalised for marker in LLAMA3_MODEL_MARKERS)


def validate_model_license_fields(claim: ReleaseClaimRecord) -> tuple[str, ...]:
    """Return missing or invalid model-license fields for fail-closed ledgers.

    Llama 3.x model references carry custom Meta community-license obligations.
    Release claims that mention those model families must preserve enough
    structured license data for NOTICE generation and later release audits.

    Returns:
        Missing or invalid model-license field names.
    """
    if not model_claim_requires_llama3_license(claim):
        return ()

    failures: list[str] = []
    required = {
        "model_id": claim.model_id,
        "model_family": claim.model_family,
        "model_license": claim.model_license,
        "model_license_url": claim.model_license_url,
        "model_license_notice": claim.model_license_notice,
    }
    for field, value in required.items():
        if not value.strip():
            failures.append(field)

    license_name = claim.model_license.lower()
    if claim.model_license and (
        LLAMA3_LICENSE_NAME_FRAGMENT not in license_name or "community license" not in license_name
    ):
        failures.append("model_license")
    if claim.model_license_url and not claim.model_license_url.startswith(("https://", "http://")):
        failures.append("model_license_url")

    return tuple(dict.fromkeys(failures))
