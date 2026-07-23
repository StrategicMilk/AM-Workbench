"""Release tooling for Vetinari.

Provides the proof artifact schema, append-only claims ledger, and utilities
used by ``scripts/release_doctor.py`` and ``scripts/pre_release_gate.py``
to record, verify, and gate release evidence before closing any release claim.

Public API:
    ReleaseProof             -- frozen dataclass representing a single release run.
    ReleaseClaimRecord       -- frozen dataclass representing one verified claim.
    ClaimKind                -- enum for claim evidence kinds.
    PROOF_SCHEMA_VERSION     -- current schema version string consumed by readers.
    ClaimsLedger             -- append-only JSONL ledger for persisting claim records.
    LedgerVerificationReport -- result of a full ledger audit pass (fail-closed).
"""

from __future__ import annotations

from vetinari.release.claims_ledger import ClaimsLedger, LedgerVerificationReport
from vetinari.release.proof_schema import PROOF_SCHEMA_VERSION, ClaimKind, ReleaseClaimRecord, ReleaseProof

__all__ = [
    "PROOF_SCHEMA_VERSION",
    "ClaimKind",
    "ClaimsLedger",
    "LedgerVerificationReport",
    "ReleaseClaimRecord",
    "ReleaseProof",
]
