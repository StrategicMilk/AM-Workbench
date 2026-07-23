"""Reachable facade for ML/GenAI practitioner mode templates."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from vetinari.constants import PROJECT_ROOT
from vetinari.workbench.modes import templates as mode_templates
from vetinari.workbench.modes.templates import BoundModeTemplate, TemplateStateRejected

ML_GENAI_MODE_MANIFEST_PATH = PROJECT_ROOT / "config" / "workbench_mode_templates" / "ml_genai_practitioner_modes.yaml"


class MlGenaiModeCatalogError(ValueError):
    """Raised when the ML/GenAI mode family is missing or stale."""


def load_ml_genai_mode_manifest(path: Path | str = ML_GENAI_MODE_MANIFEST_PATH) -> dict[str, Any]:
    """Load and validate the owned ML/GenAI mode-family manifest.

    Returns:
        Resolved ml genai mode manifest value.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    manifest_path = Path(path)
    try:
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise MlGenaiModeCatalogError(f"ML/GenAI mode manifest unreadable: {manifest_path}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise MlGenaiModeCatalogError("ML/GenAI mode manifest schema_version must be 1")
    mode_ids = raw.get("mode_ids")
    if not isinstance(mode_ids, list) or not mode_ids or not all(isinstance(item, str) and item for item in mode_ids):
        raise MlGenaiModeCatalogError("ML/GenAI mode manifest must declare non-empty mode_ids")
    capabilities = raw.get("required_capabilities")
    if not isinstance(capabilities, list) or not capabilities:
        raise MlGenaiModeCatalogError("ML/GenAI mode manifest must declare required_capabilities")
    return raw


def _manifest_mode_ids() -> tuple[str, ...]:
    return tuple(load_ml_genai_mode_manifest()["mode_ids"])


ML_GENAI_MODE_IDS = (
    "ml_genai_eval_design",
    "ml_genai_prompt_iteration",
    "ml_genai_dataset_curation",
    "ml_genai_annotation",
    "ml_genai_training_run_setup",
    "ml_genai_model_selection",
    "ml_genai_deployment_promotion_review",
    "ml_genai_incident_autopsy",
    "ml_genai_runtime_tuning",
)


def list_ml_genai_mode_templates(
    catalog_path: Path | str = mode_templates.DEFAULT_MODE_TEMPLATE_CONFIG_PATH,
) -> tuple[BoundModeTemplate, ...]:
    """Return ML/GenAI templates from the live mode-template runtime catalog.

    Returns:
        Collection of ml genai mode templates values.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    catalog = mode_templates.load_mode_template_catalog(catalog_path, use_cache=False)
    by_id = {template.template_id: template for template in catalog}
    missing = tuple(mode_id for mode_id in ML_GENAI_MODE_IDS if mode_id not in by_id)
    if missing:
        raise MlGenaiModeCatalogError(f"ML/GenAI mode templates missing from live catalog: {missing}")
    return tuple(by_id[mode_id] for mode_id in ML_GENAI_MODE_IDS)


def require_ready_ml_genai_state(template_id: str, state: dict[str, Any]) -> None:
    """Validate a selected ML/GenAI mode state through the shared runtime guard.

    Args:
        template_id: Template id value consumed by require_ready_ml_genai_state().
        state: State value consumed by require_ready_ml_genai_state().

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    templates = {template.template_id: template for template in list_ml_genai_mode_templates()}
    template = templates.get(template_id)
    if template is None:
        raise TemplateStateRejected(f"unknown ML/GenAI mode template: {template_id}")
    mode_templates.ModeTemplateRuntime(template).require_ready_state(state)


def summarize_ml_genai_mode_family() -> tuple[dict[str, object], ...]:
    """Return UI/API-friendly mode summaries without bypassing runtime parsing."""
    return tuple(
        {
            "id": template.template_id,
            "name": template.template.name,
            "input_model": template.input_model,
            "required_artifacts": template.output_contract.required_artifacts,
            "rubric_ids": tuple(criterion.criterion_id for criterion in template.review_rubric),
            "promotion_states": tuple(transition.to_state for transition in template.transitions),
            "failure_follow_up": tuple(step.user_visible_status for step in template.failure_recovery),
        }
        for template in list_ml_genai_mode_templates()
    )
