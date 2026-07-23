"""Rule-learning helpers for :mod:`vetinari.rules_manager`."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from vetinari.security.fail_closed import UntrustedInputError, sanitize_untrusted_text
from vetinari.utils.bounded_collections import BoundedList

logger = logging.getLogger("vetinari.rules_manager")

_MAX_RULE_EVIDENCE_REFS = 50
_MAX_RULES_PER_SCOPE = 500


def _safe_learning_text(value: str, *, label: str, max_length: int = 4_000) -> str:
    try:
        return sanitize_untrusted_text(value, max_length=max_length)
    except UntrustedInputError as exc:
        raise UntrustedInputError(f"{label} is not safe rules-learning input") from exc


class RulesManagerLearningMixin:
    """Provide rule proposal and correction-learning behavior."""

    if TYPE_CHECKING:
        _data: Any
        _lock: Any
        _save: Any

    def propose_rule_from_feedback(
        self,
        agent_type: str,
        mode: str,
        violation_description: str,
        model_name: str | None = None,
        evidence_ref: str | None = None,
        reviewer: str | None = None,
    ) -> bool:
        """Propose a new rule based on Quality rejection feedback.

        Args:
            agent_type: Agent type that produced the violation.
            mode: Agent mode during the violation.
            violation_description: Description of the violation pattern.
            model_name: Optional model name for model-specific rules.
            evidence_ref: Optional evidence reference for the observed violation.
            reviewer: Optional independent reviewer identifier.

        Returns:
            True if an existing proposed rule was promoted.
        """
        agent_type = _safe_learning_text(agent_type, label="agent_type", max_length=160)
        mode = _safe_learning_text(mode, label="mode", max_length=160)
        violation_description = _safe_learning_text(
            violation_description,
            label="violation_description",
            max_length=4_000,
        )
        if model_name is not None:
            model_name = _safe_learning_text(model_name, label="model_name", max_length=200)
        if evidence_ref is not None:
            evidence_ref = _safe_learning_text(evidence_ref, label="evidence_ref", max_length=1_000)
        if reviewer is not None:
            reviewer = _safe_learning_text(reviewer, label="reviewer", max_length=200)

        with self._lock:
            proposed = self._data.setdefault("proposed", {})
            rule_key = f"{agent_type}:{mode}:{violation_description}"

            if rule_key in proposed:
                proposed[rule_key]["observations"] += 1
                if evidence_ref:
                    refs = BoundedList[str](
                        _MAX_RULE_EVIDENCE_REFS,
                        proposed[rule_key].setdefault("evidence_refs", []),
                    )
                    if evidence_ref not in refs:
                        refs.append(evidence_ref)
                    proposed[rule_key]["evidence_refs"] = list(refs)
                if reviewer:
                    proposed[rule_key]["reviewer"] = reviewer
                obs_count = proposed[rule_key]["observations"]
                if obs_count >= 3:
                    if not proposed[rule_key].get("evidence_refs") or not proposed[rule_key].get("reviewer"):
                        proposed[rule_key]["status"] = "review_required"
                        self._save()
                        logger.info(
                            "Rule proposal reached %d observations but requires evidence_ref and reviewer before acceptance: %s",
                            obs_count,
                            violation_description,
                        )
                        return False
                    self._accept_proposed_rule(
                        proposed[rule_key],
                        agent_type,
                        model_name,
                    )
                    del proposed[rule_key]
                    logger.info(
                        "Rule auto-accepted after %d observations: %s",
                        obs_count,
                        violation_description,
                    )
                    self._save()
                    return True
                self._save()
                return False
            proposed[rule_key] = {
                "description": violation_description,
                "agent_type": agent_type,
                "mode": mode,
                "model_name": model_name,
                "observations": 1,
                "evidence_refs": [evidence_ref] if evidence_ref else [],
                "reviewer": reviewer,
                "status": "proposed",
            }
            logger.info(
                "New rule proposed (1/3): %s for %s:%s",
                violation_description,
                agent_type,
                mode,
            )
            self._save()
            return False

    def _accept_proposed_rule(
        self,
        proposed: dict[str, Any],
        agent_type: str,
        model_name: str | None,
    ) -> None:
        """Promote a proposed rule to the appropriate scope.

        Args:
            proposed: The proposed rule data.
            agent_type: Agent type for agent-scoped rules.
            model_name: If set, add as model-specific rule instead.
        """
        desc = proposed["description"]

        if model_name:
            models = self._data.setdefault("models", {})
            model_rules = models.setdefault(model_name, [])
            if desc not in model_rules:
                model_rules.append(desc)
                del model_rules[:-_MAX_RULES_PER_SCOPE]
        else:
            agents = self._data.setdefault("agents", {})
            agent_rules = agents.setdefault(agent_type, [])
            if desc not in agent_rules:
                agent_rules.append(desc)
                del agent_rules[:-_MAX_RULES_PER_SCOPE]
