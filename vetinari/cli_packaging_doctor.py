"""Packaging CLI — ``vetinari doctor`` diagnostic command and its diagnostic checks.

Runs a suite of health checks covering Python version, GPU/CUDA, llama-cpp-python,
vLLM/NIM runtime wiring, local models, SQLite database, config files, security
module, agent pipeline, memory store, disk space, web port, stale locks,
Thompson sampling state, training data, episode memory, and the rich output
library.

Each check returns a ``(label, status, detail)`` tuple using the constants
``_CHECK_PASS``, ``_CHECK_FAIL``, ``_CHECK_WARN``, and ``_CHECK_INFO`` from
``cli_packaging_data``. JSON-oriented checks may also attach extra structured
payload fields that are merged into the JSON output.

Imported by ``cli_packaging.py`` which re-exports ``cmd_doctor`` for the CLI.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import logging
import multiprocessing
import os
import queue
import sys
from contextlib import contextmanager, suppress
from typing import Any, cast

from vetinari.backend_config import load_backend_runtime_config
from vetinari.cli_packaging_data import (
    _CHECK_FAIL,
    _CHECK_INFO,
    _CHECK_PASS,
    _CHECK_WARN,
    _RICH_AVAILABLE,
    _print_check,
    _print_header,
)
from vetinari.cli_packaging_doctor_checks import (
    _check_agent_pipeline as _check_agent_pipeline,
)
from vetinari.cli_packaging_doctor_checks import (
    _check_backend_registration as _check_backend_registration,
)
from vetinari.cli_packaging_doctor_checks import (
    _check_config_files as _check_config_files,
)
from vetinari.cli_packaging_doctor_checks import (
    _check_cuda_readiness as _check_cuda_readiness,
)
from vetinari.cli_packaging_doctor_checks import (
    _check_cuda_toolkit as _check_cuda_toolkit,
)
from vetinari.cli_packaging_doctor_checks import (
    _check_database as _check_database,
)
from vetinari.cli_packaging_doctor_checks import (
    _check_dependency_groups as _check_dependency_groups,
)
from vetinari.cli_packaging_doctor_checks import (
    _check_dependency_readiness_matrix as _check_dependency_readiness_matrix,
)
from vetinari.cli_packaging_doctor_checks import (
    _check_disk_space as _check_disk_space,
)
from vetinari.cli_packaging_doctor_checks import (
    _check_episode_memory as _check_episode_memory,
)
from vetinari.cli_packaging_doctor_checks import (
    _check_gpu_detection as _check_gpu_detection,
)
from vetinari.cli_packaging_doctor_checks import (
    _check_memory_store as _check_memory_store,
)
from vetinari.cli_packaging_doctor_checks import (
    _check_model_loadable as _check_model_loadable,
)
from vetinari.cli_packaging_doctor_checks import (
    _check_models_directory as _check_models_directory,
)
from vetinari.cli_packaging_doctor_checks import (
    _check_nim_endpoint as _check_nim_endpoint,
)
from vetinari.cli_packaging_doctor_checks import (
    _check_notification_deliverability as _check_notification_deliverability,
)
from vetinari.cli_packaging_doctor_checks import (
    _check_runtime_matrix as _check_runtime_matrix,
)
from vetinari.cli_packaging_doctor_checks import (
    _check_security_import as _check_security_import,
)
from vetinari.cli_packaging_doctor_checks import (
    _check_stale_locks as _check_stale_locks,
)
from vetinari.cli_packaging_doctor_checks import (
    _check_thompson_state as _check_thompson_state,
)
from vetinari.cli_packaging_doctor_checks import (
    _check_training_data as _check_training_data,
)
from vetinari.cli_packaging_doctor_checks import (
    _check_vllm_endpoint as _check_vllm_endpoint,
)
from vetinari.cli_packaging_doctor_checks import (
    _check_web_port as _check_web_port,
)

logger = logging.getLogger(__name__)

_NATIVE_VERSION_PROBE_TIMEOUT_SECONDS = 20.0


def _module_is_available(module_name: str) -> bool:
    """Return True when a module can be discovered without importing it."""
    if module_name in sys.modules:
        return sys.modules[module_name] is not None
    with suppress(ModuleNotFoundError, ValueError):
        return importlib.util.find_spec(module_name) is not None
    return False


@contextmanager
def _suppress_expected_check_logs(enabled: bool):
    if not enabled:
        yield
        return
    previous = logging.root.manager.disable
    logging.disable(logging.WARNING)
    try:
        yield
    finally:
        logging.disable(previous)


def _check_hint(label: str, status: str, detail: str) -> str | None:
    """Return a structured recovery hint for actionable doctor findings."""
    if status not in {_CHECK_FAIL, _CHECK_WARN}:
        return None
    normalized = f"{label} {detail}".lower()
    if "ollama" in normalized:
        return "Remove Ollama backend config and follow docs/reference/migration-ollama.md for supported local or server backends."
    if "llama-cpp" in normalized:
        return "Install the local inference extra or configure a remote backend before running local GGUF inference."
    if "vllm" in normalized or "nim" in normalized:
        return "Start the configured backend endpoint or disable that backend in the runtime config."
    if "models directory" in normalized or "model load" in normalized or "gguf" in normalized:
        return "Run `vetinari models scan` and verify VETINARI_MODELS_DIR contains the model files you expect."
    if "database" in normalized:
        return "Check the database path permissions, then rerun `vetinari doctor --json` before starting the app."
    if "config" in normalized:
        return "Restore the missing config file or regenerate defaults with `vetinari init --dry-run` before writing state."
    if "backend registration" in normalized:
        return "Align config/backend runtime provider names with registered adapter providers, then rerun `vetinari doctor`."
    if "web port" in normalized:
        return "Stop the process using the port or start Vetinari with an explicit alternate --port value."
    if "dependency" in normalized:
        return "Install the missing optional dependency group shown in the detail, then rerun the same doctor command."
    return "Use the check label and detail to fix the failing subsystem, then rerun `vetinari doctor --json`."


# ── Individual check functions ─────────────────────────────────────────────────


def _check_python_version() -> tuple[str, str, str]:
    """Check that Python 3.10 or newer is in use.

    Returns:
        Tuple of (label, status, detail).
    """
    major, minor = sys.version_info[:2]
    ver = f"{major}.{minor}.{sys.version_info.micro}"
    if (major, minor) >= (3, 10):
        return "Python >= 3.10", _CHECK_PASS, ver
    return "Python >= 3.10", _CHECK_FAIL, f"{ver} (need >= 3.10)"


def _value_mentions_ollama(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_value_mentions_ollama(key) or _value_mentions_ollama(item) for key, item in value.items())
    if isinstance(value, (list, tuple, set)):
        return any(_value_mentions_ollama(item) for item in value)
    if isinstance(value, str):
        return value.strip().lower() == "ollama"
    return False


def _check_ollama_migration() -> tuple[str, str, str]:
    """Warn when stale Ollama backend configuration is still present."""
    env_names = ("VETINARI_OLLAMA_ENDPOINT", "VETINARI_OLLAMA_BASE_URL", "OLLAMA_HOST")
    detected: list[str] = [name for name in env_names if os.environ.get(name)]
    try:
        runtime_cfg = load_backend_runtime_config()
    except Exception as exc:
        logger.warning("Could not load backend runtime config for Ollama migration check", exc_info=True)
        return "Ollama migration", _CHECK_WARN, f"could not inspect runtime config: {exc}"
    if _value_mentions_ollama(runtime_cfg.get("inference_backend", {})):
        detected.append("inference_backend config")
    if detected:
        found = ", ".join(sorted(set(detected)))
        return (
            "Ollama migration",
            _CHECK_WARN,
            "Ollama backend support was removed; migrate to llama_cpp, LM Studio/OpenAI-compatible, "
            f"vLLM, NIM, or LiteLLM. See docs/reference/migration-ollama.md. Detected: {found}",
        )
    return "Ollama migration", _CHECK_PASS, "no removed Ollama backend configuration detected"


def _check_llama_cpp() -> tuple[str, str, str]:
    """Check that llama-cpp-python is importable.

    Returns:
        Tuple of (label, status, detail).
    """
    if importlib.util.find_spec("llama_cpp") is None:
        logger.warning("llama-cpp-python not installed - local inference unavailable")
        return "llama-cpp-python", _CHECK_FAIL, "not installed - run: pip install llama-cpp-python"
    version = _probe_native_package_version("llama_cpp")
    if version is None:
        logger.warning("llama-cpp-python native import failed in isolated probe")
        return "llama-cpp-python", _CHECK_FAIL, "installed but native import failed"
    return "llama-cpp-python", _CHECK_PASS, f"version {version}"


def _check_vllm_package() -> tuple[str, str, str]:
    """Check whether the local Python environment can import the ``vllm`` package."""
    runtime_cfg = load_backend_runtime_config()
    vllm_cfg = runtime_cfg.get("inference_backend", {}).get("vllm", {})
    endpoint = vllm_cfg.get("endpoint", "")
    if importlib.util.find_spec("vllm") is None:
        if isinstance(vllm_cfg, dict) and vllm_cfg.get("enabled") and endpoint:
            return "vLLM package", _CHECK_INFO, f"not installed in this env (endpoint mode at {endpoint})"
        logger.warning("vLLM package not installed in the local Python environment")
        return "vLLM package", _CHECK_WARN, "not installed in this env"
    version = _probe_native_package_version("vllm")
    if version is None:
        logger.warning("vLLM package native import failed in isolated probe")
        return "vLLM package", _CHECK_WARN, "installed but native import failed"
    return "vLLM package", _CHECK_PASS, f"version {version}"


def _native_version_worker(module_name: str, result_queue: Any) -> None:
    module: Any = importlib.import_module(module_name)
    result_queue.put(str(getattr(module, "__version__", "unknown")))


def _probe_native_package_version(module_name: str) -> str | None:
    ctx = multiprocessing.get_context("spawn")
    result_queue = ctx.Queue(maxsize=1)
    process = ctx.Process(target=_native_version_worker, args=(module_name, result_queue))
    try:
        process.start()
        process.join(_NATIVE_VERSION_PROBE_TIMEOUT_SECONDS)
    except Exception:
        logger.warning("native package version probe failed to start for %s", module_name, exc_info=True)
        result_queue.close()
        result_queue.join_thread()
        return None
    if process.is_alive():
        process.terminate()
        process.join(1.0)
        logger.warning("native package version probe timed out for %s", module_name)
        return None
    if process.exitcode != 0:
        logger.warning("native package version probe exited %s for %s", process.exitcode, module_name)
        return None
    try:
        return cast(str, result_queue.get_nowait())
    except queue.Empty:
        logger.warning("native package version probe returned no result for %s", module_name)
        return None
    finally:
        result_queue.close()


def _check_rich_available() -> tuple[str, str, str]:
    """Check that the rich output library is available.

    Returns:
        Tuple of (label, status, detail).
    """
    if _RICH_AVAILABLE:
        try:
            from importlib.metadata import version

            ver = version("rich")
            return "Rich (pretty output)", _CHECK_PASS, f"version {ver}"
        except Exception:
            logger.warning("Rich version detection failed — rich is available but version unknown")
            return "Rich (pretty output)", _CHECK_PASS, "available"
    return "Rich (pretty output)", _CHECK_INFO, "not installed (plain text output active)"


def _run_doctor_checks(checks: list[Any], *, verbose: bool) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for check_fn in checks:
        try:
            with _suppress_expected_check_logs(not verbose):
                result = check_fn()
            label, status, detail = result[:3]
            payload = result[3] if len(result) > 3 else None
        except Exception as exc:
            label = check_fn.__name__.replace("_check_", "").replace("_", " ").title()
            status = _CHECK_FAIL
            detail = f"unexpected error: {exc}"
            payload = None
            logger.exception("Doctor check %s raised an unexpected error", check_fn.__name__)
        item: dict[str, Any] = {"label": label, "status": status, "detail": detail}
        if isinstance(payload, dict):
            item.update(payload)
        if hint := _check_hint(label, status, detail):
            item["hint"] = hint
        results.append(item)
    return results


def _print_doctor_results(results: list[dict[str, Any]]) -> None:
    _print_header("Vetinari Doctor — System Health Check")
    for item in results:
        _print_check(item["label"], item["status"], item["detail"])
    total = len(results)
    passed = sum(1 for r in results if r["status"] == _CHECK_PASS)
    warned = sum(1 for r in results if r["status"] == _CHECK_WARN)
    failed = sum(1 for r in results if r["status"] == _CHECK_FAIL)
    info = sum(1 for r in results if r["status"] == _CHECK_INFO)
    print(f"\n  {total} checks: {passed} passed, {warned} warnings, {info} info, {failed} failed")
    if failed == 0:
        print("  All critical checks passed.")
    else:
        print(f"  {failed} check(s) require attention.")


# ── cmd_doctor ─────────────────────────────────────────────────────────────────


def cmd_doctor(args: Any) -> int:
    """Run diagnostic checks and report the health of the Vetinari installation.

    Checks cover Python version, GPU/CUDA, llama-cpp-python, models, database,
    config files, security, agent pipeline, memory, disk space, web port, lock
    files, Thompson sampling state, training data, episode memory, rich
    availability, optional dependency groups, the package readiness matrix, CUDA
    readiness, and native backend registration.

    Args:
        args: Parsed CLI arguments.  Recognises ``args.json`` (bool) to emit
            machine-readable JSON output instead of formatted text.

    Returns:
        0 if all checks passed or only warnings/info, 1 if any check failed.
    """
    checks = [
        _check_python_version,
        _check_gpu_detection,
        _check_cuda_toolkit,
        _check_ollama_migration,
        _check_llama_cpp,
        _check_vllm_package,
        _check_vllm_endpoint,
        _check_nim_endpoint,
        _check_notification_deliverability,
        _check_models_directory,
        _check_model_loadable,
        _check_database,
        _check_config_files,
        _check_security_import,
        _check_agent_pipeline,
        _check_memory_store,
        _check_disk_space,
        _check_web_port,
        _check_stale_locks,
        _check_thompson_state,
        _check_training_data,
        _check_episode_memory,
        _check_rich_available,
        _check_dependency_groups,
        _check_dependency_readiness_matrix,
        _check_cuda_readiness,
        _check_runtime_matrix,
        _check_backend_registration,
    ]

    results = _run_doctor_checks(checks, verbose=bool(getattr(args, "verbose", False)))

    use_json = getattr(args, "json", False)
    if use_json:
        print(json.dumps(results, indent=2))
    else:
        _print_doctor_results(results)

    has_failure = any(r["status"] == _CHECK_FAIL for r in results)
    return 1 if has_failure else 0
