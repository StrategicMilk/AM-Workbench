"""Utils package — shared utilities for the Vetinari codebase."""

from __future__ import annotations

import logging
import logging.handlers
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, TypeVar

import yaml

from vetinari.utils.bounded_metrics import BoundedMetrics
from vetinari.utils.frontmatter import parse_frontmatter
from vetinari.utils.lazy_import import lazy_import, require_import
from vetinari.utils.math_helpers import cosine_distance, cosine_similarity, percentile, stddev
from vetinari.utils.registry import BaseRegistry
from vetinari.utils.serialization import dataclass_to_dict
from vetinari.utils.singleton import thread_safe_singleton

T = TypeVar("T")

__all__ = [
    "BaseRegistry",
    "BoundedMetrics",
    "SingletonMeta",
    "cosine_distance",
    "cosine_similarity",
    "dataclass_to_dict",
    "estimate_model_memory_gb",
    "extract_privacy_envelope",
    "lazy_import",
    "load_config",
    "load_yaml",
    "parse_frontmatter",
    "percentile",
    "privacy_receipt",
    "require_import",
    "require_privacy_envelope",
    "setup_logging",
    "stddev",
    "thread_safe_singleton",
    "validate_required_fields",
    "wrap_privacy_envelope",
]


# ---------------------------------------------------------------------------
# Singleton helper — replaces 18+ copy-pasted _instance patterns
# ---------------------------------------------------------------------------


class SingletonMeta(type):
    """Thread-safe singleton metaclass.

    Usage::

        class MyService(metaclass=SingletonMeta):
            def __init__(self, config=None):
                self.config = config or {}

        # First call creates the instance; subsequent calls return same object.
        svc = MyService(config={"key": "value"})
        assert MyService() is svc  # True

        # Reset for testing:
        MyService.reset_instance()
    """

    _instances: dict[type, Any] = {}
    _lock = threading.Lock()

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            with cls._lock:
                if cls not in cls._instances:
                    instance = super().__call__(*args, **kwargs)
                    cls._instances[cls] = instance
        return cls._instances[cls]

    def reset_instance(cls) -> None:
        """Remove the cached singleton instance (useful for tests)."""
        with cls._lock:
            cls._instances.pop(cls, None)


def setup_logging(
    level: int = logging.INFO,
    log_dir: str = "logs",
    *,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
) -> None:
    """Configure root logging to write to both a rotating file and stdout.

    Creates ``log_dir`` if it does not already exist, then installs a
    ``FileHandler`` (append mode) and a ``StreamHandler`` on the root logger
    at the requested level.  Any existing handlers on the root logger are
    replaced so that callers can switch level or log directory at runtime.

    Args:
        level: Logging level integer (e.g. ``logging.DEBUG``, ``logging.INFO``).
        log_dir: Directory where ``vetinari.log`` will be written.
        max_bytes: Maximum size of ``vetinari.log`` before rotation.
        backup_count: Number of rotated log files to retain.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    Path(log_dir).mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    file_handler = logging.handlers.RotatingFileHandler(
        f"{log_dir}/vetinari.log",
        mode="a",
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)

    root.setLevel(level)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)


def _expand_env_vars(obj: Any) -> Any:
    """Recursively expand ${VAR} placeholders in YAML string values."""
    if isinstance(obj, str):
        return re.sub(
            r"\$\{([^}]+)\}",
            lambda m: os.environ.get(m.group(1), m.group(0)),
            obj,
        )
    if isinstance(obj, dict):
        return {k: _expand_env_vars(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_env_vars(item) for item in obj]
    return obj


def load_yaml(path: str) -> Any:
    """Load a YAML file and expand ``${ENV_VAR}`` placeholders in string values.

    Opens *path* with UTF-8 encoding, parses it with ``yaml.safe_load``, then
    recursively replaces every ``${VAR}`` token in string values with the
    corresponding environment variable (leaving the token unchanged when the
    variable is not set).

    Args:
        path: Path to the YAML file to load.

    Returns:
        The parsed and env-expanded value (typically a ``dict``).
    """
    with Path(path).open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return _expand_env_vars(data)


def load_config(path: str) -> Any:
    """Alias for load_yaml."""
    return load_yaml(path)


def estimate_model_memory_gb(model_id: str) -> int:
    """Estimate GPU memory requirement in GB from a model ID string.

    Shared utility used by model_search, model_discovery, and vram_manager.
    Returns a conservative estimate based on parameter count in the model name.

    Returns:
        Estimated GPU memory in gigabytes required to load the model at the
        assumed Q4 quantization level.
    """
    model_lower = model_id.lower()

    # Extract parameter count patterns like 70b, 72b, 7b, 3.8b, 0.5b
    match = re.search(r"(\d+(?:\.\d+)?)\s*b\b", model_lower)
    if match:
        params = float(match.group(1))
        # Q4 quantisation rule-of-thumb: ~0.55 GB per billion params
        # Add 2 GB overhead for KV cache + activations
        estimated = int(params * 0.55) + 2
        return max(2, estimated)

    return 4  # conservative default for unknown sizes


def validate_required_fields(data: dict, fields: list) -> str | None:
    """Validate that all required fields are present in a request dict.

    Args:
        data: The request data dictionary.
        fields: List of required field names.

    Returns:
        Error message string if validation fails, None if all fields present.
    """
    if not data:
        return "Request body is required"
    missing = [f for f in fields if f not in data or data[f] is None]
    if missing:
        return f"Missing required fields: {', '.join(missing)}"
    return None


PRIVACY_ENVELOPE_KEY = "_privacy_envelope"
_VALID_PRIVACY_CLASSES = {"public", "operational", "subject_data", "secret"}
_SENSITIVE_PRIVACY_CLASSES = {"subject_data", "secret"}


def _normalise_privacy_class(privacy_class: str) -> str:
    value = privacy_class.strip().lower()
    if value not in _VALID_PRIVACY_CLASSES:
        allowed = ", ".join(sorted(_VALID_PRIVACY_CLASSES))
        raise ValueError(f"unsupported privacy_class {privacy_class!r}; expected one of: {allowed}")
    return value


def _default_erasure_token(*, source: str, subject_id: str | None) -> str:
    if subject_id:
        from vetinari.privacy.erasure_registry import build_erasure_token

        return build_erasure_token(source=source, subject_id=subject_id)
    return f"{source}:operational"


def privacy_receipt(
    *,
    privacy_class: str,
    subject_id: str | None = None,
    retention_days: int = 30,
    source: str,
    erasure_token: str | None = None,
    redaction_applied: bool = False,
) -> dict[str, Any]:
    """Build a fail-closed privacy receipt for persisted or exposed data.

    Returns:
        Privacy receipt metadata suitable for embedding in a privacy envelope.

    Raises:
        ValueError: If the privacy class is unsupported, sensitive data lacks a
            subject ID, the source is blank, or retention is not positive.
    """
    normalized = _normalise_privacy_class(privacy_class)
    if normalized in _SENSITIVE_PRIVACY_CLASSES and not subject_id:
        raise ValueError(f"{normalized} records require subject_id for erasure binding")
    if not source or not str(source).strip():
        raise ValueError("privacy receipt source is required")
    if retention_days < 1:
        raise ValueError("retention_days must be positive")
    token = erasure_token or _default_erasure_token(source=source, subject_id=subject_id)
    return {
        "schema_version": "vetinari-privacy-envelope.v1",
        "privacy_class": normalized,
        "subject_id": subject_id,
        "retention_days": int(retention_days),
        "erasure_token": token,
        "source": source,
        "redaction_applied": bool(redaction_applied),
        "created_at_unix": time.time(),
    }


def wrap_privacy_envelope(
    payload: Any,
    *,
    privacy_class: str,
    subject_id: str | None = None,
    retention_days: int = 30,
    source: str,
    erasure_token: str | None = None,
    redaction_applied: bool = False,
) -> dict[str, Any]:
    """Return *payload* with a privacy receipt used by persistence boundaries."""
    return {
        "payload": payload,
        PRIVACY_ENVELOPE_KEY: privacy_receipt(
            privacy_class=privacy_class,
            subject_id=subject_id,
            retention_days=retention_days,
            source=source,
            erasure_token=erasure_token,
            redaction_applied=redaction_applied,
        ),
    }


def extract_privacy_envelope(record: dict[str, Any]) -> dict[str, Any]:
    """Return the privacy envelope from a wrapped record or raise.

    Returns:
        The validated privacy receipt stored under ``PRIVACY_ENVELOPE_KEY``.

    Raises:
        ValueError: If ``record`` is not a dictionary, the envelope is missing,
            or the embedded receipt is invalid.
    """
    if not isinstance(record, dict):
        raise ValueError("privacy envelope record must be a dict")
    envelope = record.get(PRIVACY_ENVELOPE_KEY)
    if not isinstance(envelope, dict):
        raise ValueError("privacy envelope missing")
    privacy_receipt(
        privacy_class=str(envelope.get("privacy_class", "")),
        subject_id=envelope.get("subject_id"),
        retention_days=int(envelope.get("retention_days", 0)),
        source=str(envelope.get("source", "")),
        erasure_token=envelope.get("erasure_token"),
        redaction_applied=bool(envelope.get("redaction_applied", False)),
    )
    return envelope


def require_privacy_envelope(record: dict[str, Any]) -> dict[str, Any]:
    """Validate that *record* carries a readable fail-closed privacy envelope.

    Returns:
        The original record after its privacy envelope has been validated.

    Raises:
        ValueError: If the record has no valid privacy envelope.
    """
    extract_privacy_envelope(record)
    return record
