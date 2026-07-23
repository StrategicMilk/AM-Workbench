"""JSON fallback helpers for the plan-tracking memory store."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vetinari.privacy import PRIVACY_ENVELOPE_KEY, wrap_for_persistence

logger = logging.getLogger("vetinari.memory.plan_tracking")

_MAX_JSON_PLANS = 1_000
_MAX_JSON_SUBTASKS = 5_000
_MAX_JSON_MODEL_PERFORMANCE = 1_000
_MAX_JSON_PRUNE_RECEIPTS = 1_000


class PlanTrackingJsonMixin:
    """Read and write plan-tracking records in the JSON fallback store."""

    if TYPE_CHECKING:
        _json_data: Any
        _json_path: Any

    def _init_json_store(self) -> None:
        self.use_json_fallback = True
        self._json_recovery_needed = False
        if not Path(self._json_path).exists():
            self._json_data = {"plans": {}, "subtasks": {}, "model_performance": {}, "prune_receipts": {}}
            self._save_json()
        else:
            try:
                with Path(self._json_path).open(encoding="utf-8") as f:
                    loaded = json.load(f)
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning(
                    "JSON fallback memory store at %s is unreadable or corrupt; write recovery is required",
                    self._json_path,
                )
                logger.debug("JSON fallback load failure: %s", exc)
                self._json_recovery_needed = True
                loaded = {}
            self._json_data = {
                "plans": _trim_mapping(dict(loaded.get("plans", {})), _MAX_JSON_PLANS)
                if isinstance(loaded, dict)
                else {},
                "subtasks": _trim_mapping(dict(loaded.get("subtasks", {})), _MAX_JSON_SUBTASKS)
                if isinstance(loaded, dict)
                else {},
                "model_performance": _trim_mapping(
                    dict(loaded.get("model_performance", {})),
                    _MAX_JSON_MODEL_PERFORMANCE,
                )
                if isinstance(loaded, dict)
                else {},
                "prune_receipts": _trim_mapping(dict(loaded.get("prune_receipts", {})), _MAX_JSON_PRUNE_RECEIPTS)
                if isinstance(loaded, dict)
                else {},
            }
        logger.info("JSON fallback memory store initialized at %s", self._json_path)

    def _json_writable(self) -> bool:
        if getattr(self, "_json_recovery_needed", False):
            logger.error(
                "JSON fallback memory store at %s requires explicit recovery before writes",
                self._json_path,
            )
            return False
        return True

    def _save_json(self) -> None:
        if not self._json_writable():
            raise RuntimeError("JSON fallback memory store requires explicit recovery before writes")
        path = Path(self._json_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f".{path.name}.tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(self._json_data, f, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        tmp_path.replace(path)

    def _write_plan_json(self, plan_data: dict[str, Any]) -> bool:
        if not self._json_writable():
            return False
        plan_id = plan_data.get("plan_id")
        self._json_data["plans"][plan_id] = _privacy_enveloped_record(
            {**plan_data, "updated_at": datetime.now(timezone.utc).isoformat()},
            source="memory.plan_tracking_json.plan",
            subject_id=str(plan_id or "unknown-plan"),
        )
        self._trim_json_store()
        self._save_json()
        return True

    def _write_subtask_json(self, subtask_data: dict[str, Any]) -> bool:
        if not self._json_writable():
            return False
        subtask_id = subtask_data.get("subtask_id")
        self._json_data["subtasks"][subtask_id] = _privacy_enveloped_record(
            {
                **subtask_data,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            source="memory.plan_tracking_json.subtask",
            subject_id=str(subtask_data.get("plan_id") or subtask_id or "unknown-subtask"),
        )
        self._trim_json_store()
        self._save_json()
        return True

    def _query_plan_json(self, plan_id: str | None, goal_contains: str | None, limit: int) -> list[dict[str, Any]]:
        plans = list(self._json_data["plans"].values())

        if plan_id:
            plans = [p for p in plans if p.get("plan_id") == plan_id]
        elif goal_contains:
            plans = [p for p in plans if goal_contains.lower() in p.get("goal", "").lower()]

        plans.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        return plans[:limit]

    def _query_subtasks_json(
        self,
        plan_id: str | None,
        subtask_id: str | None,
        depth: int | None,
    ) -> list[dict[str, Any]]:
        subtasks = list(self._json_data["subtasks"].values())

        if subtask_id:
            subtasks = [s for s in subtasks if s.get("subtask_id") == subtask_id]
        elif plan_id:
            subtasks = [s for s in subtasks if s.get("plan_id") == plan_id]
            if depth is not None:
                subtasks = [s for s in subtasks if s.get("depth") == depth]

        return subtasks

    def _update_model_perf_json(self, model_id: str, task_type: str, success: bool, latency: float) -> bool:
        if not self._json_writable():
            return False
        key = f"{model_id}:{task_type}"
        if key not in self._json_data["model_performance"]:
            self._json_data["model_performance"][key] = {
                "model_id": model_id,
                "task_type": task_type,
                "success_rate": 0.0,
                "avg_latency": 0.0,
                "total_uses": 0,
            }

        perf = self._json_data["model_performance"][key]
        total = perf["total_uses"] + 1
        perf["success_rate"] = (perf["success_rate"] * perf["total_uses"] + (1 if success else 0)) / total
        perf["avg_latency"] = (perf["avg_latency"] * perf["total_uses"] + latency) / total
        perf["total_uses"] = total
        perf["last_used_at"] = datetime.now(timezone.utc).isoformat()
        self._json_data["model_performance"][key] = _privacy_enveloped_record(
            perf,
            source="memory.plan_tracking_json.model_performance",
            subject_id=key,
        )

        self._trim_json_store()
        self._save_json()
        return True

    def _trim_json_store(self) -> None:
        self._json_data["plans"] = _trim_mapping(self._json_data.get("plans", {}), _MAX_JSON_PLANS)
        self._json_data["subtasks"] = _trim_mapping(self._json_data.get("subtasks", {}), _MAX_JSON_SUBTASKS)
        self._json_data["model_performance"] = _trim_mapping(
            self._json_data.get("model_performance", {}),
            _MAX_JSON_MODEL_PERFORMANCE,
        )
        self._json_data["prune_receipts"] = _trim_mapping(
            self._json_data.get("prune_receipts", {}),
            _MAX_JSON_PRUNE_RECEIPTS,
        )


def _trim_mapping(values: dict[Any, Any], max_items: int) -> dict[Any, Any]:
    if len(values) <= max_items:
        return dict(values)
    keys = tuple(values.keys())[-max_items:]
    return {key: values[key] for key in keys}


def _privacy_enveloped_record(record: dict[str, Any], *, source: str, subject_id: str) -> dict[str, Any]:
    wrapped = wrap_for_persistence(
        dict(record),
        privacy_class="subject_data",
        subject_id=subject_id,
        retention_days=30,
        source=source,
        redaction_applied=False,
    )
    return {**record, PRIVACY_ENVELOPE_KEY: wrapped[PRIVACY_ENVELOPE_KEY]}
