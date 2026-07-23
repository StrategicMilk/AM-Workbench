"""Feedback replay and quality-rejection helpers for FeedbackLoop."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from vetinari.boundary_guards import account_evidence_drop
from vetinari.types import AgentType

logger = logging.getLogger("vetinari.learning.feedback_loop")


class FeedbackLoopReplayMixin:
    """Mixin for persisted feedback replay and learned rule proposals."""

    def record_quality_rejection(
        self,
        agent_type: str,
        mode: str,
        violation_description: str,
        model_name: str | None = None,
        evidence_ref: str | None = None,
        reviewer: str | None = None,
    ) -> None:
        """Record a Quality rejection and propose a rule if pattern is new.

        Bridges Quality agent feedback into the RulesManager rule learning
        system.  After 3 consistent observations of the same violation, a rule
        is auto-accepted.

        Args:
            agent_type: Agent type that produced the rejected output.
            mode: Agent mode during the rejection.
            violation_description: Short description of the violation.
            model_name: Optional model name for model-specific rules.
            evidence_ref: Durable source proving this rejection observation.
            reviewer: Human or trusted review authority behind the rejection.

        Raises:
            Exception: Propagates rule-manager failures after accounting the
                dropped feedback evidence.
        """
        try:
            from vetinari.rules_manager import get_rules_manager

            rules = get_rules_manager()
            accepted = rules.propose_rule_from_feedback(
                agent_type=agent_type,
                mode=mode,
                violation_description=violation_description,
                model_name=model_name,
                evidence_ref=evidence_ref,
                reviewer=reviewer,
            )
            if accepted:
                logger.info(
                    "Quality feedback auto-accepted as rule: %s",
                    violation_description,
                )
        except Exception:
            item = {
                "agent_type": agent_type,
                "mode": mode,
                "violation_description": violation_description,
                "evidence_ref": evidence_ref,
            }
            account_evidence_drop(item, "feedback_loop_replay", logger=logger)
            logger.error("Rule proposal from feedback failed", exc_info=True)
            raise

    def load_feedback_jsonl(self, feedback_path: str | Path) -> int:
        """Load and replay user feedback from a persisted JSONL file.

        Reads every line from ``feedback_path``, converts each thumbs-up/down
        record into a quality signal, and feeds it into the Thompson Sampling
        and rule-learning subsystems.  This bridges the gap between the disk
        file written by ``chat_api.submit_feedback`` and the in-memory learning
        systems so that feedback survives process restarts.

        Args:
            feedback_path: Path to the ``feedback.jsonl`` file produced by
                ``chat_api.submit_feedback``.

        Returns:
            Number of feedback records successfully replayed.
        """
        path = Path(feedback_path)
        if not path.exists():
            logger.debug("[FeedbackLoop] No feedback file found at %s — skipping replay", path)
            return 0

        replayed = 0
        try:
            from vetinari.learning.model_selector import get_thompson_selector

            thompson = get_thompson_selector()
            with path.open(encoding="utf-8") as fh:
                for line_no, line in enumerate(fh, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        logger.warning("[FeedbackLoop] Skipping malformed JSON at line %d in %s", line_no, path)
                        continue

                    rating = record.get("rating", "")
                    if rating not in ("up", "down"):
                        continue

                    quality = 0.9 if rating == "up" else 0.2
                    model_id = record.get("model_id", "default")
                    task_type = record.get("task_type", "general")

                    thompson.update(model_id, task_type, quality, success=(rating == "up"))

                    # On rejection, propose a rule from the stored comment
                    if rating == "down":
                        comment = record.get("comment") or f"User rejected task {record.get('task_id', 'unknown')}"
                        self.record_quality_rejection(
                            agent_type=record.get("agent_type", AgentType.WORKER.value),
                            mode="user_feedback",
                            violation_description=str(comment),
                            model_name=model_id,
                            evidence_ref=f"{path}:{line_no}",
                            reviewer=str(record.get("reviewer") or record.get("user_id") or "user_feedback"),
                        )
                    replayed += 1

            logger.info("[FeedbackLoop] Replayed %d feedback record(s) from %s", replayed, path)
        except Exception:
            logger.warning("[FeedbackLoop] Failed to load feedback from %s", feedback_path, exc_info=True)

        return replayed
