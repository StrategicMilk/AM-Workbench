"""Evidence-related canonical enums re-exported by :mod:`vetinari.types`."""

from __future__ import annotations

from enum import Enum


class EvidenceBasis(str, Enum):
    """Canonical classification of what kind of evidence backs an OutcomeSignal."""

    TOOL_EVIDENCE = "tool_evidence"
    LLM_JUDGMENT = "llm_judgment"
    HUMAN_ATTESTED = "human_attested"
    HYBRID = "hybrid"
    UNSUPPORTED = "unsupported"


class ArtifactKind(str, Enum):
    """The kind of concrete artifact that backs a human attestation."""

    COMMAND_INVOCATION = "command_invocation"
    COMMIT_SHA = "commit_sha"
    SIGNED_REVIEW = "signed_review"
    ADR_REFERENCE = "adr_reference"
    EXTERNAL_RECEIPT = "external_receipt"
