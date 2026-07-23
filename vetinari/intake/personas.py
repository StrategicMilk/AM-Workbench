"""Persona bundle resolver for layering user intent profiles over RequestFrame values."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any, Literal

import yaml

from vetinari.intake.request_frame import RequestFrame

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_BUNDLES_PATH = _REPO_ROOT / "config" / "persona_bundles.yaml"
_VALID_MODEL_TIERS = frozenset({"standard", "fast", "quality"})
_VALID_URGENCY = frozenset({"low", "medium", "high"})


class WorkerModeCluster(str, Enum):
    """Worker mode clusters that persona bundles may target."""

    CODE = "code"
    ANALYSIS = "analysis"
    PLAN = "plan"
    REFACTOR = "refactor"
    TEST = "test"
    INFRA = "infra"
    DOCS = "docs"
    SPIKE = "spike"


@dataclass(frozen=True, slots=True)
class PersonaBundle:
    """Typed persona profile resolved before Worker dispatch."""

    name: str
    description: str
    worker_mode_cluster: list[WorkerModeCluster]
    preferred_model_tier: str
    urgency_default: Literal["low", "medium", "high"]
    budget_tokens_default: int
    request_frame_overrides: dict[str, Any]

    def __repr__(self) -> str:
        modes = [mode.value for mode in self.worker_mode_cluster]
        return f"PersonaBundle(name={self.name!r}, worker_mode_cluster={modes!r})"


class PersonaResolver:
    """Resolve named persona bundles and apply their RequestFrame defaults."""

    def __init__(self, bundles_path: Path | None = None) -> None:
        """Load and validate persona bundles once.

        Args:
            bundles_path: Optional path to a persona bundle YAML file.

        Raises:
            ValueError: If the YAML file contains invalid bundle data.
        """
        self._bundles_path = bundles_path or _DEFAULT_BUNDLES_PATH
        self._bundles = self._load_bundles(self._bundles_path)

    def resolve(self, persona_name: str) -> PersonaBundle:
        """Return the bundle for a persona name.

        Args:
            persona_name: Persona key to resolve.

        Returns:
            Matching PersonaBundle.

        Raises:
            ValueError: If the persona name is unknown.
        """
        try:
            return self._bundles[persona_name]
        except KeyError as exc:
            valid_names = sorted(self._bundles)
            raise ValueError(f"Unknown persona: {persona_name!r}. Valid names: {valid_names}") from exc

    def apply(self, frame: RequestFrame, persona_name: str) -> RequestFrame:
        """Apply persona defaults to a RequestFrame without mutating it.

        Args:
            frame: Base frame produced by IntakeParser.
            persona_name: Persona key to overlay.

        Returns:
            A new RequestFrame with valid persona overrides applied.
        """
        bundle = self.resolve(persona_name)
        overrides: dict[str, Any] = {
            "persona_name": bundle.name,
            "preferred_worker_mode": bundle.worker_mode_cluster[0].value,
            "preferred_model_tier": bundle.preferred_model_tier,
            "urgency": bundle.urgency_default,
            "budget_tokens": bundle.budget_tokens_default,
        }
        overrides.update(bundle.request_frame_overrides)
        allowed_fields = RequestFrame.__dataclass_fields__
        filtered = {key: value for key, value in overrides.items() if key in allowed_fields}
        if frame.destructive_intent and "destructive_intent" not in filtered:
            filtered["destructive_intent"] = True
        return replace(frame, **filtered)

    def _load_bundles(self, bundles_path: Path) -> dict[str, PersonaBundle]:
        with bundles_path.open("r", encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle)
        if not isinstance(loaded, dict):
            raise ValueError("Persona bundles YAML must be a mapping")
        raw_bundles = loaded.get("bundles")
        if not isinstance(raw_bundles, dict) or not raw_bundles:
            raise ValueError("Persona bundles YAML must define bundles")
        return {name: self._build_bundle(name, raw) for name, raw in raw_bundles.items()}

    @staticmethod
    def _build_bundle(name: str, raw: object) -> PersonaBundle:
        if not isinstance(raw, dict):
            raise ValueError(f"Persona bundle {name!r} must be a mapping")
        raw_modes = raw.get("worker_mode_cluster")
        if not isinstance(raw_modes, list) or not raw_modes:
            raise ValueError(f"Persona bundle {name!r} must define non-empty worker_mode_cluster")
        try:
            modes = [WorkerModeCluster(mode) for mode in raw_modes]
        except ValueError as exc:
            raise ValueError(f"Persona bundle {name!r} has invalid worker_mode_cluster value") from exc
        tier = raw.get("preferred_model_tier")
        urgency = raw.get("urgency_default")
        token_budget = raw.get("budget_tokens_default")
        overrides = raw.get("request_frame_overrides", {})
        if tier not in _VALID_MODEL_TIERS:
            raise ValueError(f"Persona bundle {name!r} has invalid preferred_model_tier")
        if urgency not in _VALID_URGENCY:
            raise ValueError(f"Persona bundle {name!r} has invalid urgency_default")
        if not isinstance(token_budget, int) or token_budget <= 0:
            raise ValueError(f"Persona bundle {name!r} has invalid budget_tokens_default")
        if not isinstance(overrides, dict):
            raise ValueError(f"Persona bundle {name!r} request_frame_overrides must be a mapping")
        description = raw.get("description")
        if not isinstance(description, str) or not description.strip():
            raise ValueError(f"Persona bundle {name!r} must define a description")
        return PersonaBundle(
            name=name,
            description=description,
            worker_mode_cluster=modes,
            preferred_model_tier=tier,
            urgency_default=urgency,
            budget_tokens_default=token_budget,
            request_frame_overrides=overrides,
        )


__all__ = ["PersonaBundle", "PersonaResolver", "WorkerModeCluster"]
