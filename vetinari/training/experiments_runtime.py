"""Training experiment support helpers retained outside the Python web layer."""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import yaml

from vetinari.config_paths import resolve_config_path

logger = logging.getLogger(__name__)


_RULES_CONFIG_NAME = "training_automation_rules.yaml"
_UPLOAD_DIR_ENV = "VETINARI_TRAINING_UPLOAD_DIR"

# Maximum upload size for training data (50 MB).
_MAX_TRAINING_UPLOAD_BYTES = 50 * 1024 * 1024

# Lock protecting _automation_rules list mutations and lazy initialization.
_automation_rules_lock = threading.Lock()

# Module-level rule store â€” loaded lazily on first access, not at import time.
_automation_rules: list[dict[str, Any]] | None = None

# Permitted file extensions for training data uploads.
_ALLOWED_EXTENSIONS = frozenset({".jsonl", ".json"})


def _rules_path() -> Path:
    """Return the active automation-rules path lazily for source and package layouts."""
    return resolve_config_path(_RULES_CONFIG_NAME)


def _upload_dir() -> Path:
    """Return the training upload directory without baking a source-tree path at import."""
    override = os.environ.get(_UPLOAD_DIR_ENV)
    if override:
        return Path(override).expanduser().resolve()
    return Path.cwd().resolve() / "outputs" / "training_uploads"


def _load_automation_rules() -> list[dict[str, Any]]:
    """Load automation rules from the YAML persistence file.

    Returns an empty list when the file does not exist yet or when it contains
    no rules.  Logs a warning and returns an empty list on parse errors so that
    the API endpoint remains callable even after a corrupted write.

    Returns:
        The list of rule dicts stored in the YAML file, or an empty list.
    """
    rules_path = _rules_path()
    if not rules_path.exists():
        return []
    try:
        with rules_path.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        if isinstance(data, list):
            return data
        logger.warning("_load_automation_rules: unexpected YAML structure, resetting rules")
        return []
    except yaml.YAMLError as exc:
        logger.warning("_load_automation_rules: YAML parse error: %s", exc)
        return []


def _save_automation_rules(rules: list[dict[str, Any]]) -> None:
    """Persist automation rules to the YAML file on disk.

    Creates the ``config/`` directory if it does not already exist.

    Args:
        rules: The full list of rule dicts to write.

    Raises:
        OSError: If the file cannot be written due to permission or I/O errors.
    """
    rules_path = _rules_path()
    rules_path.parent.mkdir(parents=True, exist_ok=True)
    with rules_path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(rules, fh, default_flow_style=False, allow_unicode=True)


def _get_automation_rules() -> list[dict[str, Any]]:
    """Return the automation rules list, loading from disk on first call.

    Uses double-checked locking so that disk I/O happens at most once per
    process lifetime even under concurrent requests.

    Returns:
        The list of automation rule dicts, possibly empty if no rules exist.
    """
    global _automation_rules
    if _automation_rules is None:
        with _automation_rules_lock:
            if _automation_rules is None:
                _automation_rules = _load_automation_rules()
    return _automation_rules


def _upsert_automation_rule(rule: dict[str, Any]) -> tuple[str, dict[str, Any], int]:
    """Create or update a training automation rule in the persisted store."""
    global _automation_rules
    rule_id: str | None = rule.get("id") or rule.get("name")
    _get_automation_rules()
    with _automation_rules_lock:
        if _automation_rules is None:
            _automation_rules = []
        rules = _automation_rules
        if rule_id:
            for index, existing in enumerate(rules):
                if (existing.get("id") or existing.get("name")) == rule_id:
                    rules[index] = rule
                    _save_automation_rules(rules)
                    logger.info("create_automation_rule: updated rule id=%s", rule_id)
                    return "updated", rule, len(rules)
        rules.append(rule)
        _save_automation_rules(rules)
        logger.info("create_automation_rule: created rule (total=%d)", len(rules))
        return "created", rule, len(rules)


async def _generate_training_progress_events() -> AsyncGenerator[dict[str, Any], None]:
    """Yield SSE frames for the live training-progress stream.

    Emits ``training_status`` events for status transitions and
    ``training_progress`` events containing per-step metrics while a training
    run is active.  A keepalive comment is emitted every 25 seconds to prevent
    proxies from closing an idle connection.

    Yields:
        Dicts with ``event`` + ``data`` keys for status/progress frames, or
        ``comment`` key for keepalives, matching the runtime SSE event shape.
    """
    import asyncio

    from vetinari.training.api_runtime import _get_scheduler, _is_scheduler_training

    if not _is_scheduler_training():
        yield {"event": "training_status", "data": json.dumps({"status": "idle"})}
        return

    last_heartbeat = time.monotonic()
    from vetinari.types import StatusEnum

    last_status: str = StatusEnum.RUNNING.value
    yield {"event": "training_status", "data": json.dumps({"status": last_status})}

    try:
        while _is_scheduler_training():
            now = time.monotonic()

            # Emit current progress snapshot from the real scheduler job if available.
            scheduler = _get_scheduler()
            if scheduler is not None:
                job = scheduler.current_job
                if job is not None:
                    progress_snapshot = {
                        "job_id": job.job_id,
                        "activity_description": job.activity_description,
                        "progress": job.progress,
                    }
                    yield {"event": "training_progress", "data": json.dumps(progress_snapshot)}

            # Heartbeat every 25 seconds to keep the connection alive.
            if now - last_heartbeat >= 25:
                yield {"comment": "keepalive"}
                last_heartbeat = now

            await asyncio.sleep(2)

        # Training just finished - emit final status.
        yield {"event": "training_status", "data": json.dumps({"status": "finished"})}
    finally:
        parent = sys.modules.get("vetinari.training.experiments_runtime")
        parent_logger = getattr(parent, "logger", logger)
        parent_logger.debug("Training progress SSE stream closed")
