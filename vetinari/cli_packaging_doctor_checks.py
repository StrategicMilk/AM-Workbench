"""Diagnostic check implementations for :mod:`vetinari.cli_packaging_doctor`."""

from __future__ import annotations

import importlib
import importlib.util
import logging
import shutil
import socket
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

from vetinari.backend_config import load_backend_runtime_config, resolve_provider_fallback_order
from vetinari.cli_packaging_data import _CHECK_FAIL, _CHECK_INFO, _CHECK_PASS, _CHECK_WARN, DEFAULT_USER_MODELS_DIR
from vetinari.constants import get_user_dir
from vetinari.security.redaction import redact_text

logger = logging.getLogger("vetinari.cli_packaging_doctor")


def _module_is_available(module_name: str) -> bool:
    """Return True when a module can be discovered without importing it."""
    import sys

    if module_name in sys.modules:
        return sys.modules[module_name] is not None
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ModuleNotFoundError, ValueError) as exc:
        logger.debug("Module availability probe failed for %s: %s", module_name, exc)
        return False


def _check_gpu_detection() -> tuple[str, str, str]:
    """Check GPU availability via pynvml or nvidia-smi fallback."""
    if _module_is_available("pynvml"):
        try:
            pynvml: Any = importlib.import_module("pynvml")

            pynvml.nvmlInit()
            count = pynvml.nvmlDeviceGetCount()
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            name = pynvml.nvmlDeviceGetName(handle)
            if isinstance(name, bytes):
                name = name.decode("utf-8", errors="replace")
            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
            vram_gb = round(mem.total / (1024**3), 1)
            pynvml.nvmlShutdown()
            return "GPU detection (pynvml)", _CHECK_PASS, f"{count}x {name} ({vram_gb} GB VRAM)"
        except Exception as exc:
            logger.warning(
                "GPU detection via pynvml failed: %s — falling back to nvidia-smi probe, GPU may still be available",
                exc,
            )
            return "GPU detection (pynvml)", _CHECK_WARN, f"pynvml error: {exc}"

    # Fallback: nvidia-smi subprocess
    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi:
        return "GPU detection (nvidia-smi)", _CHECK_PASS, "nvidia-smi found"
    return "GPU detection", _CHECK_WARN, "no GPU detected (CPU inference only)"


def _check_cuda_toolkit() -> tuple[str, str, str]:
    """Check that CUDA toolkit tools are reachable on PATH."""
    nvcc = shutil.which("nvcc")
    if nvcc:
        return "CUDA toolkit (nvcc)", _CHECK_PASS, Path(nvcc).name
    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi:
        return "CUDA toolkit (nvidia-smi only)", _CHECK_WARN, "nvcc not found, runtime only"
    return "CUDA toolkit", _CHECK_WARN, "not found (CPU inference still works)"


def _check_openai_compatible_endpoint(label: str, endpoint: str) -> tuple[str, str, str]:
    """Check whether an OpenAI-compatible ``/v1/models`` endpoint is reachable."""
    if not endpoint:
        return label, _CHECK_INFO, "not configured"

    safe_endpoint = redact_text(endpoint)
    try:
        import httpx

        resp = httpx.get(f"{endpoint.rstrip('/')}/v1/models", timeout=5)
        resp.raise_for_status()
        model_count = len(resp.json().get("data", []))
        return label, _CHECK_PASS, f"{safe_endpoint} ({model_count} model(s))"
    except Exception as exc:
        logger.warning("%s check failed for %s: %s", label, safe_endpoint, redact_text(str(exc)))
        return label, _CHECK_WARN, f"{safe_endpoint} unreachable: {redact_text(str(exc))}"


def _check_vllm_endpoint() -> tuple[str, str, str]:
    """Check the configured vLLM endpoint."""
    runtime_cfg = load_backend_runtime_config()
    vllm_cfg = runtime_cfg.get("inference_backend", {}).get("vllm", {})
    endpoint = vllm_cfg.get("endpoint", "") if isinstance(vllm_cfg, dict) else ""
    if not (isinstance(vllm_cfg, dict) and vllm_cfg.get("enabled")):
        return "vLLM endpoint", _CHECK_INFO, "not enabled"
    return _check_openai_compatible_endpoint("vLLM endpoint", str(endpoint))


def _check_nim_endpoint() -> tuple[str, str, str]:
    """Check the configured NVIDIA NIM endpoint."""
    runtime_cfg = load_backend_runtime_config()
    nim_cfg = runtime_cfg.get("inference_backend", {}).get("nim", {})
    endpoint = nim_cfg.get("endpoint", "") if isinstance(nim_cfg, dict) else ""
    if not (isinstance(nim_cfg, dict) and nim_cfg.get("enabled")):
        return "NIM endpoint", _CHECK_INFO, "not enabled"
    return _check_openai_compatible_endpoint("NIM endpoint", str(endpoint))


def _check_notification_deliverability() -> tuple[str, str, str]:
    """Check configured outbound notification routes without sending messages."""
    try:
        import yaml

        from vetinari.config_paths import resolve_config_path

        config_path = resolve_config_path("notifications.yaml")
        if not config_path.exists():
            return "Outbound notifications", _CHECK_INFO, "notifications.yaml not configured"
        payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        webhooks = payload.get("webhooks", []) if isinstance(payload, dict) else []
        enabled = [item for item in webhooks if isinstance(item, dict) and item.get("enabled", True)]
        count = len(enabled)
        if count:
            suffix = "webhook" if count == 1 else "webhooks"
            return "Outbound notifications", _CHECK_PASS, f"{count} enabled {suffix} configured"
        return "Outbound notifications", _CHECK_INFO, "no enabled webhooks configured"
    except Exception as exc:
        logger.warning("Notification deliverability check failed: %s", exc)
        return "Outbound notifications", _CHECK_WARN, f"check error: {exc}"


def _check_backend_registration() -> tuple[str, str, str]:
    """Check that all configured backends are registered with the adapter manager.

    Returns FAIL (not just WARN) when a provider named in the runtime config is absent
    from the adapter manager, because inference will break at runtime for those requests.
    """
    try:
        from vetinari.adapter_manager import get_adapter_manager

        runtime_cfg = load_backend_runtime_config()
        manager = get_adapter_manager()
        providers = set(manager.list_providers())
        expected = set(resolve_provider_fallback_order(runtime_cfg))
        missing = sorted(expected - providers)
        order = manager.get_status().get("fallback_order", [])
        if missing:
            # Configured but unregistered providers mean inference will fail at runtime.
            logger.warning(
                "Configured backend provider(s) not registered — inference will fail: %s",
                ", ".join(missing),
            )
            return "Backend registration", _CHECK_FAIL, f"configured but not registered: {', '.join(missing)}"
        if not order:
            return "Backend registration", _CHECK_WARN, "no fallback order configured"
        return "Backend registration", _CHECK_PASS, f"registered={sorted(providers)} fallback={order}"
    except Exception as exc:
        logger.warning("Backend registration check failed: %s", exc)
        return "Backend registration", _CHECK_FAIL, f"check error: {exc}"


def _check_models_directory() -> tuple[str, str, str]:
    """Check that the models directory exists and contains at least one .gguf file."""
    from vetinari.constants import DEFAULT_MODELS_DIR

    dirs_to_check = [DEFAULT_USER_MODELS_DIR, Path(DEFAULT_MODELS_DIR)]
    existing_dirs: list[Path] = []
    for models_dir in dirs_to_check:
        if models_dir.exists():
            existing_dirs.append(models_dir)
            gguf_files = list(models_dir.rglob("*.gguf"))
            if gguf_files:
                return (
                    "Models directory",
                    _CHECK_PASS,
                    f"{len(gguf_files)} .gguf file(s) in {redact_text(str(models_dir))}",
                )
    if existing_dirs:
        names = ", ".join(redact_text(str(path)) for path in existing_dirs)
        return "Models directory", _CHECK_WARN, f"directories exist but no .gguf files in {names}"
    checked = " and ".join(redact_text(str(path)) for path in dirs_to_check)
    return "Models directory", _CHECK_FAIL, f"not found — checked {checked}"


def _check_model_loadable() -> tuple[str, str, str]:
    """Check that at least one .gguf file has a valid GGUF header (magic bytes).

    Only reads the first 4 bytes of each candidate file to avoid loading the full
    model.  Continues past unreadable files (permissions, corruption) rather than
    stopping at the first error — a single bad file should not mask healthy ones.

    """
    from vetinari.constants import DEFAULT_MODELS_DIR

    gguf_magic = b"GGUF"
    dirs_to_check = [DEFAULT_USER_MODELS_DIR, Path(DEFAULT_MODELS_DIR)]
    unreadable: list[str] = []
    bad_header: list[str] = []
    for models_dir in dirs_to_check:
        for gguf_path in models_dir.rglob("*.gguf"):
            try:
                with gguf_path.open("rb") as fh:
                    header = fh.read(4)
                if header == gguf_magic:
                    return "Model file header", _CHECK_PASS, f"{gguf_path.name} has valid GGUF header"
                bad_header.append(gguf_path.name)
            except OSError as exc:
                logger.warning("Could not read model file %s for header check — skipping: %s", gguf_path.name, exc)
                unreadable.append(gguf_path.name)
    # No valid GGUF found — report what we found
    if bad_header:
        return "Model file header", _CHECK_WARN, f"unexpected header bytes in: {', '.join(bad_header[:3])}"
    if unreadable:
        return "Model file header", _CHECK_WARN, f"could not read: {', '.join(unreadable[:3])}"
    return "Model file header", _CHECK_WARN, "no .gguf files found to validate"


def _check_database() -> tuple[str, str, str]:
    """Check the unified SQLite database is accessible and has the expected schema tables.

    Verifies the connection succeeds and that the core unified-schema tables exist.
    The canonical tables are ``execution_state``, ``memories``, and ``sse_event_log``,
    which are always created by ``vetinari.database.init_schema``.  A missing table
    indicates the database was created by a pre-migration schema; run ``vetinari migrate``
    to update it.

    Returns:
        Tuple of (label, status, detail).
    """
    # Core tables always present in the live unified DB after init_schema runs.
    # Source of truth: vetinari/database.py CREATE TABLE blocks.
    _UNIFIED_TABLES = {"execution_state", "memories", "sse_event_log"}
    if not _module_is_available("vetinari.database"):
        logger.warning("vetinari.database module not importable — database check skipped")
        return "SQLite database", _CHECK_FAIL, "vetinari.database not importable"
    try:
        from vetinari.database import get_connection

        with get_connection() as conn:
            cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            present = {row[0] for row in cursor.fetchall()}
        missing_tables = _UNIFIED_TABLES - present
        if missing_tables:
            missing_str = ", ".join(sorted(missing_tables))
            logger.warning(
                "SQLite database is accessible but missing unified-schema tables: %s — run 'vetinari migrate'",
                missing_str,
            )
            return (
                "SQLite database",
                _CHECK_WARN,
                f"connected but missing tables: {missing_str} — run 'vetinari migrate'",
            )
        return "SQLite database", _CHECK_PASS, "connection OK, unified schema present"
    except ModuleNotFoundError as exc:
        logger.warning("vetinari.database module import failed — database check skipped: %s", exc)
        return "SQLite database", _CHECK_FAIL, f"import error: {exc}"
    except Exception as exc:
        logger.warning("SQLite database connection failed — check database file: %s", exc)
        return "SQLite database", _CHECK_FAIL, str(exc)


def _check_config_files() -> tuple[str, str, str]:
    """Check that required YAML config files exist in the project config/ directory.

    Returns:
        Tuple of (label, status, detail).
    """
    from vetinari.config_paths import resolve_config_path

    required = ["ml_config.yaml", "models.yaml"]
    missing = [p for p in required if not resolve_config_path(p).exists()]
    if not missing:
        return "Config files", _CHECK_PASS, ", ".join(f"config/{p}" for p in required)
    return "Config files", _CHECK_FAIL, f"missing: {', '.join(f'config/{p}' for p in missing)}"


def _check_security_import() -> tuple[str, str, str]:
    """Check that vetinari.security is importable.

    Returns:
        Tuple of (label, status, detail).
    """
    if not _module_is_available("vetinari.security"):
        logger.warning("Security module not found — security features unavailable")
        return "Security module", _CHECK_FAIL, "vetinari.security not importable"
    importlib.import_module("vetinari.security")
    return "Security module", _CHECK_PASS, "vetinari.security importable"


def _check_agent_pipeline() -> tuple[str, str, str]:
    """Check that the two-layer orchestration pipeline is importable.

    Returns:
        Tuple of (label, status, detail).
    """
    if not _module_is_available("vetinari.orchestration.two_layer"):
        logger.warning("Agent pipeline module not found — orchestration unavailable")
        return "Agent pipeline", _CHECK_FAIL, "vetinari.orchestration.two_layer not importable"
    importlib.import_module("vetinari.orchestration.two_layer")
    return "Agent pipeline", _CHECK_PASS, "vetinari.orchestration.two_layer importable"


def _check_memory_store() -> tuple[str, str, str]:
    """Check that the unified memory store is importable.

    Returns:
        Tuple of (label, status, detail).
    """
    if not _module_is_available("vetinari.memory.unified"):
        logger.warning("Memory store module not found — unified memory unavailable")
        return "Memory store", _CHECK_FAIL, "vetinari.memory.unified not importable"
    importlib.import_module("vetinari.memory.unified")
    return "Memory store", _CHECK_PASS, "vetinari.memory.unified importable"


def _check_disk_space() -> tuple[str, str, str]:
    """Check that at least 1 GB of free disk space is available.

    Returns:
        Tuple of (label, status, detail).
    """
    try:
        usage = shutil.disk_usage(Path.home())
        free_gb = usage.free / (1024**3)
        if free_gb >= 1.0:
            return "Disk space", _CHECK_PASS, f"{free_gb:.1f} GB free"
        return "Disk space", _CHECK_WARN, f"only {free_gb:.1f} GB free (recommend >= 1 GB)"
    except OSError as exc:
        logger.warning("Could not determine disk space — OS error: %s", exc)
        return "Disk space", _CHECK_WARN, f"could not determine: {exc}"


def _check_web_port() -> tuple[str, str, str]:
    """Check whether the default web port (5000) is available.

    Returns:
        Tuple of (label, status, detail).
    """
    from vetinari.constants import DEFAULT_WEB_PORT

    port = DEFAULT_WEB_PORT
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        result = sock.connect_ex(("127.0.0.1", port))
    if result != 0:
        return f"Web port {port}", _CHECK_PASS, "port is available"
    return f"Web port {port}", _CHECK_WARN, f"port {port} is already in use"


def _check_stale_locks() -> tuple[str, str, str]:
    """Check for stale runtime lock files in the Vetinari state directories.

    Excludes well-known project artifact lock files (uv.lock, poetry.lock,
    package-lock.json) that are not runtime state — only Vetinari-owned .lock
    files indicate a potentially stale exclusive-access lock.

    Returns:
        Tuple of (label, status, detail).
    """
    # Project artifact lockfiles are expected and healthy — do not flag them.
    _ARTIFACT_LOCK_NAMES = {"uv.lock", "poetry.lock", "package-lock.json", "Pipfile.lock", "yarn.lock"}
    lock_files = [p for p in get_user_dir().glob("*.lock") if p.name not in _ARTIFACT_LOCK_NAMES]
    project_root = Path(__file__).resolve().parent.parent
    lock_files += [p for p in project_root.glob("*.lock") if p.name not in _ARTIFACT_LOCK_NAMES]
    if not lock_files:
        return "Stale lock files", _CHECK_PASS, "none found"
    names = ", ".join(p.name for p in lock_files[:5])
    return "Stale lock files", _CHECK_WARN, f"found: {names}"


def _check_thompson_state() -> tuple[str, str, str]:
    """Check whether Thompson sampling state files exist in the user directory.

    Returns:
        Tuple of (label, status, detail).
    """
    ts_files = list(get_user_dir().glob("thompson*.json")) + list(get_user_dir().glob("*.thompson"))
    if ts_files:
        return "Thompson sampling state", _CHECK_PASS, f"{len(ts_files)} state file(s)"
    project_state = Path(__file__).resolve().parent.parent / ".vetinari"
    ts_files_proj = list(project_state.glob("thompson*.json")) if project_state.exists() else []
    if ts_files_proj:
        return "Thompson sampling state", _CHECK_PASS, f"{len(ts_files_proj)} state file(s) in .vetinari/"
    return "Thompson sampling state", _CHECK_INFO, "no prior state (will be created on first run)"


def _check_training_data() -> tuple[str, str, str]:
    """Check that the training data store is accessible.

    Returns:
        Tuple of (label, status, detail).
    """
    if not _module_is_available("vetinari.learning.training_data"):
        logger.warning("Training data module not importable")
        return "Training data store", _CHECK_FAIL, "vetinari.learning.training_data not importable"
    try:
        from vetinari.learning.training_data import get_training_collector

        collector = get_training_collector()
        stats = collector.get_stats()
        total = stats.get("total_records", 0)
        return "Training data store", _CHECK_PASS, f"{total} records"
    except ModuleNotFoundError as exc:
        logger.warning("Training data module import failed: %s", exc)
        return "Training data store", _CHECK_FAIL, f"import error: {exc}"
    except Exception as exc:
        logger.warning("Training data store accessible but stats query failed: %s", exc)
        return "Training data store", _CHECK_WARN, f"accessible but stats unavailable: {exc}"


def _check_episode_memory() -> tuple[str, str, str]:
    """Check that the episode memory module is accessible.

    Returns:
        Tuple of (label, status, detail).
    """
    if not _module_is_available("vetinari.learning.episode_memory"):
        logger.warning("Episode memory module not importable")
        return "Episode memory", _CHECK_FAIL, "vetinari.learning.episode_memory not importable"
    try:
        from vetinari.learning.episode_memory import EpisodeMemory

        mem = EpisodeMemory()
        stats = mem.get_stats()
        if "error" in stats:
            logger.warning("Episode memory accessible but stats query failed: %s", stats["error"])
            return "Episode memory", _CHECK_WARN, f"accessible but stats unavailable: {stats['error']}"
        count = stats.get("total_episodes", 0)
        return "Episode memory", _CHECK_PASS, f"accessible ({count} stored episode(s))"
    except ModuleNotFoundError as exc:
        logger.warning("Episode memory module import failed: %s", exc)
        return "Episode memory", _CHECK_FAIL, f"import error: {exc}"
    except Exception as exc:
        logger.warning("Episode memory accessible but query failed: %s", exc)
        return "Episode memory", _CHECK_WARN, f"accessible but query failed: {exc}"


def _check_dependency_groups() -> tuple[str, str, str]:
    """Check how many optional dependency groups are fully installed.

    Returns:
        Tuple of (label, status, detail).
    """
    try:
        from vetinari.preflight import check_dependency_groups, detect_hardware

        hardware = detect_hardware()
        groups = check_dependency_groups(hardware)
        complete = sum(1 for g in groups if g.is_complete)
        total = len(groups)
        missing_rec = [g for g in groups if g.recommended and not g.is_complete]

        if not missing_rec:
            return "Dependency groups", _CHECK_PASS, f"{complete}/{total} installed (all recommended present)"
        names = ", ".join(g.extra for g in missing_rec)
        return "Dependency groups", _CHECK_WARN, f"{complete}/{total} installed — missing recommended: {names}"
    except Exception as exc:
        logger.warning("Dependency group check failed: %s", exc)
        return "Dependency groups", _CHECK_FAIL, f"check error: {exc}"


def _check_dependency_readiness_matrix() -> tuple[str, str, str, dict[str, Any]]:
    """Return a machine-readable package readiness matrix for the current environment."""
    try:
        from vetinari.preflight import (
            build_dependency_readiness_matrix,
            detect_hardware,
            summarize_dependency_readiness,
        )

        hardware = detect_hardware()
        matrix = build_dependency_readiness_matrix(hardware)
        summary = summarize_dependency_readiness(matrix)

        missing_required = cast(list[str], summary["missing_required"])
        missing_recommended = cast(list[str], summary["missing_recommended"])
        installed_unverified = cast(list[str], summary["installed_unverified"])

        if missing_required:
            status = _CHECK_FAIL
        elif missing_recommended or installed_unverified:
            status = _CHECK_WARN
        else:
            status = _CHECK_PASS

        detail_parts = [f"{summary['installed']}/{summary['total']} installed"]
        if missing_required:
            detail_parts.append(f"missing required: {', '.join(missing_required)}")
        if missing_recommended:
            detail_parts.append(f"missing recommended: {', '.join(missing_recommended)}")
        if installed_unverified:
            detail_parts.append(f"installed but unverified: {', '.join(installed_unverified)}")

        return (
            "Dependency readiness matrix",
            status,
            " — ".join(detail_parts),
            {
                "matrix": [asdict(item) for item in matrix],
                "summary": summary,
            },
        )
    except Exception as exc:
        logger.warning("Dependency readiness matrix check failed: %s", exc)
        return (
            "Dependency readiness matrix",
            _CHECK_FAIL,
            f"check error: {exc}",
            {"matrix": [], "summary": {"error": str(exc)}},
        )


def _check_cuda_readiness() -> tuple[str, str, str]:
    """Check whether CUDA support is fully configured for NVIDIA hardware.

    Only meaningful when an NVIDIA GPU is detected.

    Returns:
        Tuple of (label, status, detail).
    """
    try:
        from vetinari.preflight import check_cuda_readiness, detect_hardware

        hardware = detect_hardware()
        if not hardware.has_nvidia_gpu:
            return "CUDA readiness", _CHECK_INFO, "no NVIDIA GPU detected"

        actions = check_cuda_readiness(hardware)
        if not actions:
            return "CUDA readiness", _CHECK_PASS, "fully configured"
        return "CUDA readiness", _CHECK_WARN, f"{len(actions)} action(s) needed — run 'vetinari start' for details"
    except Exception as exc:
        logger.warning("CUDA readiness check failed: %s", exc)
        return "CUDA readiness", _CHECK_FAIL, f"check error: {exc}"


def _check_runtime_matrix() -> tuple[str, str, str]:
    """Run the supported-matrix runtime doctor (SESSION-03 SHARD-01).

    Loads ``config/runtime/supported_matrix.yaml`` and verifies detected
    runtime versions (torch, vllm, bitsandbytes, python) against declared
    minimums and known-bad ranges. Fails closed via the runtime doctor;
    this wrapper maps the resulting exit code into the existing doctor
    tuple format so the check appears alongside other health checks.

    Returns:
        Tuple of (label, status, detail).
    """
    try:
        from vetinari.runtime.runtime_doctor import cli_doctor_report

        exit_code, rendered = cli_doctor_report()
        first_line = rendered.splitlines()[0] if rendered else "no output"
        if exit_code == 0:
            return "Runtime supported matrix", _CHECK_PASS, "all components satisfy matrix"
        if exit_code == 1:
            return "Runtime supported matrix", _CHECK_WARN, f"advisory: {first_line}"
        return "Runtime supported matrix", _CHECK_FAIL, f"blocker: {first_line}"
    except FileNotFoundError as exc:
        logger.warning(
            "Runtime supported-matrix file missing — proceeding without matrix check: %s",
            exc,
        )
        return "Runtime supported matrix", _CHECK_WARN, f"matrix not configured: {exc}"
    except Exception as exc:
        logger.warning("Runtime supported-matrix check failed: %s", exc)
        return "Runtime supported matrix", _CHECK_FAIL, f"check error: {exc}"
