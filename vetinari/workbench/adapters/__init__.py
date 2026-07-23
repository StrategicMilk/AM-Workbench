"""Workbench adapter layer."""

from __future__ import annotations

from vetinari.workbench.adapters.authority import (
    AdapterAuthorityContract,
    AdapterAuthorityDecision,
    AdapterAuthorityError,
    AdapterDecisionReason,
    AdapterDirection,
    AdapterDomain,
    AdapterExchange,
    AdapterOperation,
    AdapterRoundTripResult,
    AuthorityMode,
    ConflictBehavior,
    ExternalAdapterRecord,
    Lossiness,
    PrivacyPolicy,
    ProvenanceMapping,
    StaleDataPolicy,
    assess_authority,
    build_authority_contract,
    export_adapter_record,
    prove_round_trip,
)

__all__ = [
    "AdapterAuthorityContract",
    "AdapterAuthorityDecision",
    "AdapterAuthorityError",
    "AdapterDecisionReason",
    "AdapterDirection",
    "AdapterDomain",
    "AdapterExchange",
    "AdapterOperation",
    "AdapterRoundTripResult",
    "AuthorityMode",
    "ConflictBehavior",
    "ExternalAdapterRecord",
    "Lossiness",
    "PrivacyPolicy",
    "ProvenanceMapping",
    "StaleDataPolicy",
    "assess_authority",
    "build_authority_contract",
    "export_adapter_record",
    "prove_round_trip",
]
