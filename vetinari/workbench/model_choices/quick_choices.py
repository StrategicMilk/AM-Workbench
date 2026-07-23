"""Build fail-closed Workbench model quick-choice catalogs."""

from __future__ import annotations

from datetime import datetime, timezone

from vetinari.workbench.model_choices.config_loader import QuickChoicesConfig
from vetinari.workbench.model_choices.contracts import (
    CapabilitySnapshot,
    InactiveReason,
    ModelQuickChoice,
    ProviderQualifiedModelRef,
    QuickChoiceCatalog,
    Surface,
)
from vetinari.workbench.model_registry import (
    DeprecationState,
    ModelCard,
    ModelStage,
    ModelVersion,
    RegistrySnapshot,
    WorkbenchModelRegistry,
    WorkbenchModelRegistryError,
)

MEDIA_GENERATION_CAPABILITIES = frozenset({"image_generation", "video_generation", "audio_generation"})


class QuickChoicesServiceError(RuntimeError):
    """Raised when the quick-choice service cannot build a safe catalog."""


class QuickChoicesService:
    """Surface-aware catalog builder over the public model registry snapshot."""

    def __init__(
        self,
        registry: WorkbenchModelRegistry,
        config: QuickChoicesConfig,
        multimodal_config: object | None = None,
    ) -> None:
        self._registry = registry
        self._config = config
        self._multimodal_config = multimodal_config

    def build_catalog(self, surface: Surface | str) -> QuickChoiceCatalog:
        """Build a fail-closed catalog for ``surface``.

        Returns:
            Newly constructed catalog value.

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        resolved = surface if isinstance(surface, Surface) else Surface(str(surface))
        generated_at = _utc_now_iso()
        try:
            snapshot = self._registry.snapshot()
        except WorkbenchModelRegistryError as exc:
            raise QuickChoicesServiceError(str(exc)) from exc

        choices = tuple(
            self._choice_from_version(resolved, version, snapshot, generated_at) for version in snapshot.versions
        )
        return QuickChoiceCatalog(
            surface=resolved,
            choices=tuple(sorted(choices, key=lambda choice: _sort_key(choice, self._config))),
            generated_at_utc=generated_at,
        )

    def _choice_from_version(
        self,
        surface: Surface,
        version: ModelVersion,
        snapshot: RegistrySnapshot,
        captured_at_utc: str,
    ) -> ModelQuickChoice:
        cards = {card.card_id: card for card in snapshot.cards}
        card = cards.get(version.card_id)
        if card is None:
            return _unavailable_choice(
                surface=surface,
                version=version,
                provider="unknown",
                display_name=version.model_id,
                reasons=(InactiveReason.CAPABILITY_SNAPSHOT_UNAVAILABLE,),
                evidence_refs=(f"missing-card:{version.card_id}",),
            )
        capability_snapshot = _snapshot_from(card, version, captured_at_utc)
        reasons = _inactive_reasons(surface, card, version, self._config)
        return ModelQuickChoice(
            surface=surface,
            display_name=card.display_name,
            model_ref=ProviderQualifiedModelRef(provider=card.provider, model_id=card.model_id),
            capability_snapshot=capability_snapshot,
            is_active=not reasons,
            inactive_reasons=reasons,
            evidence_refs=tuple(card.evidence_ids),
            pinned_version_id=version.version_id,
        )


def _snapshot_from(card: ModelCard, version: ModelVersion, captured_at_utc: str) -> CapabilitySnapshot:
    return CapabilitySnapshot(
        capabilities=tuple(card.capabilities),
        provider=card.provider,
        stage=version.stage.value,
        deprecation_state=version.deprecation_state.value,
        card_id=card.card_id,
        version_id=version.version_id,
        captured_at_utc=captured_at_utc,
    )


def _inactive_reasons(
    surface: Surface,
    card: ModelCard,
    version: ModelVersion,
    config: QuickChoicesConfig,
) -> tuple[InactiveReason, ...]:
    surface_config = config.surface_config(surface)
    capabilities = set(card.capabilities)
    reasons: list[InactiveReason] = []
    if not capabilities:
        reasons.append(InactiveReason.CAPABILITY_SNAPSHOT_UNAVAILABLE)
    if not set(surface_config.required_capabilities).issubset(capabilities):
        reasons.append(InactiveReason.MISSING_CAPABILITY)
    if version.stage is not ModelStage.SERVING:
        reasons.append(InactiveReason.STAGE_NOT_SERVING)
    if version.deprecation_state in {DeprecationState.SCHEDULED, DeprecationState.DEPRECATED}:
        reasons.append(InactiveReason.DEPRECATED)
    if surface_config.provider_priority and card.provider not in surface_config.provider_priority:
        reasons.append(InactiveReason.PROVIDER_DISABLED)
    if surface is Surface.CHAT and capabilities & MEDIA_GENERATION_CAPABILITIES:
        reasons.append(InactiveReason.MEDIA_NOT_ALLOWED_ON_CHAT)
    if capabilities & set(surface_config.disallowed_capabilities):
        reasons.append(
            InactiveReason.MEDIA_NOT_ALLOWED_ON_CHAT if surface is Surface.CHAT else InactiveReason.BLOCKED_BY_POLICY
        )
    return tuple(dict.fromkeys(reasons))


def _unavailable_choice(
    *,
    surface: Surface,
    version: ModelVersion,
    provider: str,
    display_name: str,
    reasons: tuple[InactiveReason, ...],
    evidence_refs: tuple[str, ...],
) -> ModelQuickChoice:
    return ModelQuickChoice(
        surface=surface,
        display_name=display_name,
        model_ref=ProviderQualifiedModelRef(provider=provider, model_id=version.model_id),
        capability_snapshot=None,
        is_active=False,
        inactive_reasons=reasons,
        evidence_refs=evidence_refs,
        pinned_version_id=version.version_id,
    )


def _sort_key(choice: ModelQuickChoice, config: QuickChoicesConfig) -> tuple[int, str, str]:
    providers = config.surface_config(choice.surface).provider_priority
    try:
        provider_index = providers.index(choice.model_ref.provider)
    except ValueError:
        provider_index = len(providers)
    return (0 if choice.is_active else 1, f"{provider_index:04d}", choice.model_ref.qualified_id)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = ["QuickChoicesService", "QuickChoicesServiceError"]
