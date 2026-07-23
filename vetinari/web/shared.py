"""Shared web runtime helpers."""

from __future__ import annotations

import logging
import queue
import re
import threading
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any, TypeVar

import yaml

from vetinari.constants import _PROJECT_ROOT
from vetinari.security.redaction import redact_text
from vetinari.web.config import get_config
from vetinari.web.sse_event_store import _persist_sse_event

logger = logging.getLogger(__name__)

T = TypeVar("T")

PROJECT_ROOT: Path = _PROJECT_ROOT


class _LazyWebConfig:
    def __init__(self) -> None:
        self._value: Any | None = None
        self._lock = threading.Lock()

    def get(self) -> Any:
        """Return the cached web configuration, loading it on first access.

        Returns:
            The shared web configuration object.
        """
        if self._value is None:
            with self._lock:
                if self._value is None:
                    self._value = get_config()
        return self._value

    def __getattr__(self, name: str) -> Any:
        return getattr(self.get(), name)

    def to_dict(self) -> dict[str, Any]:
        return self.get().to_dict()


class _LazyConfigBool:
    def __init__(self, config: _LazyWebConfig, attr: str) -> None:
        self._config = config
        self._attr = attr

    def __bool__(self) -> bool:
        return bool(getattr(self._config.get(), self._attr))

    def __repr__(self) -> str:
        return repr(bool(self))


current_config = _LazyWebConfig()
ENABLE_EXTERNAL_DISCOVERY = _LazyConfigBool(current_config, "enable_external_discovery")

_SINGLETONS: dict[type[Any], tuple[tuple[Any, ...], tuple[tuple[str, Any], ...], Any]] = {}
_LOCK = threading.Lock()
_orchestrator: Any | None = None
orchestrator: Any | None = None
_orchestrator_lock = threading.Lock()
_models_cache: list[dict[str, Any]] | None = None
_models_cache_lock = threading.Lock()
# Project route helpers read project.yaml frequently. The cache is keyed by the
# resolved config path and invalidated by file stat changes or explicit writes.
_project_config_cache: dict[Path, tuple[tuple[int, int], dict[str, Any]]] = {}
_project_config_cache_lock = threading.Lock()

_SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_SENSITIVE_SSE_KEYS = frozenset({"api_key", "apikey", "api-token", "api_token", "password", "secret", "token"})
_MAX_SAFE_PATH_PARAM_LENGTH = 128
_sse_streams: dict[str, queue.Queue[dict[str, Any] | None]] = {}
_sse_sequence_counters: dict[str, int] = {}
_sse_dropped_counts: dict[str, int] = {}
_sse_streams_lock = threading.Lock()
_cancel_flags: dict[str, threading.Event] = {}
_cancel_flags_lock = threading.Lock()


def get_singleton(cls: type[T], *args: Any, **kwargs: Any) -> T:
    """Return one singleton per class and reject conflicting constructor arguments.

    Returns:
        The cached or newly-created singleton instance.

    Raises:
        ValueError: If the singleton already exists with different arguments.
    """
    key = cls
    signature = (args, tuple(sorted(kwargs.items())))
    with _LOCK:
        existing = _SINGLETONS.get(key)
        if existing is not None:
            existing_args, existing_kwargs, instance = existing
            if existing_args != args or existing_kwargs != signature[1]:
                raise ValueError(f"singleton {cls.__name__} already exists with different constructor arguments")
            return instance
        instance = cls(*args, **kwargs)
        _SINGLETONS[key] = (args, signature[1], instance)
        return instance


def reset_singletons() -> None:
    """Clear shared singleton state for tests."""
    with _LOCK:
        _SINGLETONS.clear()


def validate_path_param(value: str | None) -> str | None:
    """Return a safe path parameter or ``None`` when it is unsafe.

    Args:
        value: Raw single-segment path parameter.

    Returns:
        The original safe value, or ``None`` for traversal, separator, empty,
        overlong, or control-character input.
    """
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or ".." in candidate or "/" in candidate or "\\" in candidate:
        return None
    if len(candidate) > _MAX_SAFE_PATH_PARAM_LENGTH:
        return None
    if not _SAFE_PATH_RE.fullmatch(candidate):
        return None
    return candidate


def set_orchestrator(orchestrator: Any | None) -> None:
    """Set the process-wide orchestrator reference.

    Args:
        orchestrator: Orchestrator instance, or ``None`` to clear it.
    """
    global _orchestrator
    with _orchestrator_lock:
        _orchestrator = orchestrator
        globals()["orchestrator"] = orchestrator


def get_orchestrator() -> Any | None:
    """Return the process-wide orchestrator reference.

    Returns:
        The current orchestrator, or ``None`` when unset.
    """
    with _orchestrator_lock:
        return _orchestrator


def _register_project_task(project_id: str) -> threading.Event:
    """Register a cancellable project task and return its flag."""
    with _cancel_flags_lock:
        flag = _cancel_flags.get(project_id)
        if flag is None:
            flag = threading.Event()
            _cancel_flags[project_id] = flag
        return flag


def _cancel_project_task(project_id: str) -> bool:
    """Set a project's cancellation flag when it exists."""
    with _cancel_flags_lock:
        flag = _cancel_flags.get(project_id)
        if flag is None:
            return False
        flag.set()
        return True


def _is_project_actually_running(project_id: str) -> bool:
    """Return whether a project has a live, uncancelled task flag."""
    with _cancel_flags_lock:
        flag = _cancel_flags.get(project_id)
        return flag is not None and not flag.is_set()


def _get_sse_queue(project_id: str) -> queue.Queue[dict[str, Any] | None]:
    """Return the SSE queue for ``project_id``, creating it when needed."""
    with _sse_streams_lock:
        stream = _sse_streams.get(project_id)
        if stream is None:
            stream = queue.Queue(maxsize=1000)
            _sse_streams[project_id] = stream
            _sse_sequence_counters.setdefault(project_id, 0)
            _sse_dropped_counts.setdefault(project_id, 0)
        return stream


def _durable_sse_sequence_floor(project_id: str) -> int:
    """Return the highest persisted sequence for a project, or zero."""
    try:
        from vetinari.database import get_connection

        conn = get_connection()
        row = conn.execute(
            "SELECT COALESCE(MAX(sequence_num), 0) AS max_sequence FROM sse_event_log WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        return int(row["max_sequence"] if row is not None else 0)
    except Exception:
        logger.warning("Failed to read durable SSE sequence floor for project %s", project_id, exc_info=True)
        return 0


def _cleanup_sse_stream_state(project_id: str) -> bool:
    """Remove only ephemeral SSE queue state for a project."""
    with _sse_streams_lock:
        existed = project_id in _sse_streams
        _sse_streams.pop(project_id, None)
        _sse_sequence_counters.pop(project_id, None)
        _sse_dropped_counts.pop(project_id, None)
        return existed


def _cleanup_project_state(project_id: str) -> bool:
    """Remove cancellable task and SSE state for a project."""
    stream_existed = _cleanup_sse_stream_state(project_id)
    with _cancel_flags_lock:
        flag_existed = project_id in _cancel_flags
        _cancel_flags.pop(project_id, None)
    return stream_existed or flag_existed


def _push_sse_event(project_id: str, event_type: str, data: dict[str, Any]) -> None:
    """Publish an SSE event to the live queue and durable replay table."""
    sanitized_data = _sanitize_sse_payload(data)
    with _sse_streams_lock:
        current_sequence = max(_sse_sequence_counters.get(project_id, 0), _durable_sse_sequence_floor(project_id))
        sequence = current_sequence + 1
        _sse_sequence_counters[project_id] = sequence
        stream = _sse_streams.get(project_id)
        if stream is None:
            stream = queue.Queue(maxsize=1000)
            _sse_streams[project_id] = stream
            _sse_dropped_counts.setdefault(project_id, 0)
        event = {"id": str(sequence), "sequence_num": sequence, "type": event_type, "data": sanitized_data}
        try:
            stream.put_nowait(event)
        except queue.Full:
            _sse_dropped_counts[project_id] = _sse_dropped_counts.get(project_id, 0) + 1
            logger.warning(
                "SSE queue full for project %s; dropped event type=%s sequence=%s dropped_count=%s",
                project_id,
                event_type,
                sequence,
                _sse_dropped_counts[project_id],
            )

    _persist_sse_event(project_id, event_type, sanitized_data, sequence_num=sequence)


def _sanitize_sse_payload(value: Any, *, max_string_chars: int = 2048) -> Any:
    if isinstance(value, str):
        return redact_text(value)[:max_string_chars]
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text.lower() in _SENSITIVE_SSE_KEYS:
                sanitized[key_text] = "[redacted]"
            else:
                sanitized[key_text] = _sanitize_sse_payload(item, max_string_chars=max_string_chars)
        return sanitized
    if isinstance(value, list | tuple):
        return [_sanitize_sse_payload(item, max_string_chars=max_string_chars) for item in value[:100]]
    return deepcopy(value)


def _derive_project_status(
    configured_status: str,
    planned_tasks: list[dict[str, Any]],
    completed_task_ids: set[str],
    *,
    project_id: str = "",
) -> str:
    """Derive project status from config and task completion evidence."""
    status = (configured_status or "unknown").lower()
    if status == "running":
        return "running" if _is_project_actually_running(project_id) else "interrupted"
    if status != "unknown":
        return status

    task_ids = {str(task.get("id", "")) for task in planned_tasks if task.get("id")}
    if not task_ids:
        return "pending"
    if task_ids.issubset(completed_task_ids):
        return "completed"
    if completed_task_ids.intersection(task_ids):
        return "in_progress"
    return "pending"


def _infer_recommended_tasks(capabilities: list[str]) -> list[str]:
    """Infer human-readable task labels from model capability strings."""
    mapping = {
        "code_gen": "coding",
        "chat": "conversation",
        "docs": "documentation",
        "vision": "vision",
        "tool_use": "tool use",
    }
    return [label for capability, label in mapping.items() if capability in capabilities]


def _get_models_cached(*, force: bool = False) -> list[dict[str, Any]]:
    """Return cached local model metadata from the model pool."""
    global _models_cache
    with _models_cache_lock:
        if _models_cache is not None and not force:
            return list(_models_cache)
        from vetinari.models.model_pool import ModelPool

        pool = ModelPool(current_config.to_dict())
        pool.discover_models()
        models = pool.list_models()
        _models_cache = [
            model.to_dict()
            if hasattr(model, "to_dict")
            else dict(model)
            if isinstance(model, dict)
            else {"name": str(model)}
            for model in models
        ]
        return list(_models_cache)


def load_project_config(
    config_path: Path,
    *,
    fail_closed: bool = False,
    error_default: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Load a project.yaml mapping through the shared route-safe cache.

    Args:
        config_path: Path to the project ``project.yaml`` file.
        fail_closed: When true, unreadable or invalid YAML returns
            ``error_default`` instead of raising. Use this for security and
            discovery decisions where unknown state must not enable access.
        error_default: Mapping returned on load failure when ``fail_closed`` is
            true. Defaults to an empty mapping.

    Returns:
        Parsed project configuration as a fresh mutable dict.

    Raises:
        ValueError: If the file cannot be read, parsed, or does not contain a
            YAML mapping and ``fail_closed`` is false.
    """
    path = Path(config_path).resolve()
    try:
        stat = path.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
        with _project_config_cache_lock:
            cached = _project_config_cache.get(path)
            if cached is not None and cached[0] == signature:
                return deepcopy(cached[1])

        with path.open(encoding="utf-8") as stream:
            loaded = yaml.safe_load(stream) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"Expected YAML mapping in {path}")

        config = dict(loaded)
        with _project_config_cache_lock:
            _project_config_cache[path] = (signature, deepcopy(config))
        return deepcopy(config)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        with _project_config_cache_lock:
            _project_config_cache.pop(path, None)
        if fail_closed:
            logger.warning(
                "Could not load project config from %s; using fail-closed defaults",
                path,
                exc_info=True,
            )
            return dict(error_default or {})
        raise ValueError(f"Could not load project config from {path}") from exc


def invalidate_project_config_cache(config_path: Path) -> None:
    """Remove one project config from the shared route cache.

    Args:
        config_path: Path to the project ``project.yaml`` file whose cached
            value should be discarded after a write.
    """
    path = Path(config_path).resolve()
    with _project_config_cache_lock:
        _project_config_cache.pop(path, None)


def _project_external_model_enabled(project_dir: Path) -> bool:
    """Return whether a project permits external model discovery."""
    config_path = project_dir / "project.yaml"
    if not config_path.exists():
        return True
    loaded = load_project_config(
        config_path,
        fail_closed=True,
        error_default={"external_model_discovery": False},
    )
    value = loaded.get("external_model_discovery", loaded.get("enable_external_discovery", True))
    return bool(value)


__all__ = [
    "ENABLE_EXTERNAL_DISCOVERY",
    "PROJECT_ROOT",
    "_cancel_flags",
    "_cancel_flags_lock",
    "_cancel_project_task",
    "_cleanup_project_state",
    "_cleanup_sse_stream_state",
    "_derive_project_status",
    "_durable_sse_sequence_floor",
    "_get_models_cached",
    "_get_sse_queue",
    "_infer_recommended_tasks",
    "_is_project_actually_running",
    "_project_external_model_enabled",
    "_push_sse_event",
    "_register_project_task",
    "_sse_dropped_counts",
    "_sse_sequence_counters",
    "_sse_streams",
    "_sse_streams_lock",
    "current_config",
    "get_orchestrator",
    "get_singleton",
    "invalidate_project_config_cache",
    "load_project_config",
    "orchestrator",
    "reset_singletons",
    "set_orchestrator",
    "validate_path_param",
]
