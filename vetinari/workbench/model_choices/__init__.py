"""Workbench model quick-choice public surface."""

from __future__ import annotations

from vetinari.workbench.model_choices.config_loader import (
    QuickChoicesConfig,
    QuickChoicesConfigError,
    SurfaceChoiceConfig,
    load_quick_choices_config,
)
from vetinari.workbench.model_choices.contracts import (
    CapabilitySnapshot,
    InactiveReason,
    ModelQuickChoice,
    ProviderQualifiedModelRef,
    QuickChoiceCatalog,
    Surface,
)
from vetinari.workbench.model_choices.provider_governance import (
    ProviderFeatureProfile,
    ProviderGovernanceDecision,
    ProviderGovernanceRequest,
    ProviderGovernanceStatus,
    evaluate_provider_governance,
)
from vetinari.workbench.model_choices.quick_choices import QuickChoicesService, QuickChoicesServiceError
from vetinari.workbench.model_choices.repin import RepinDecision, safe_repin

__all__ = [
    "CapabilitySnapshot",
    "InactiveReason",
    "ModelQuickChoice",
    "ProviderFeatureProfile",
    "ProviderGovernanceDecision",
    "ProviderGovernanceRequest",
    "ProviderGovernanceStatus",
    "ProviderQualifiedModelRef",
    "QuickChoiceCatalog",
    "QuickChoicesConfig",
    "QuickChoicesConfigError",
    "QuickChoicesService",
    "QuickChoicesServiceError",
    "RepinDecision",
    "Surface",
    "SurfaceChoiceConfig",
    "evaluate_provider_governance",
    "load_quick_choices_config",
    "safe_repin",
]
