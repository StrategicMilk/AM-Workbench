"""First-run setup wizard for Vetinari onboarding."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from rich.console import Console

from vetinari.constants import (
    DEFAULT_MODELS_DIR,
    DEFAULT_NATIVE_MODELS_DIR,
    get_user_dir,
)
from vetinari.setup.init_wizard_models import _download_model, _scan_for_models
from vetinari.setup.init_wizard_state import clear_wizard_state, load_wizard_state, save_wizard_state
from vetinari.setup.model_recommender import ModelRecommender
from vetinari.setup.vllm_container import (
    VLLMContainerPlan,
    plan_vllm_container_setup,
    start_vllm_container,
)
from vetinari.setup.vllm_container import is_openai_endpoint_ready as is_vllm_endpoint_ready
from vetinari.system.hardware_detect import GpuVendor, HardwareProfile, detect_hardware

if TYPE_CHECKING:
    from vetinari.setup.nim_container import NIMContainerPlan

logger = logging.getLogger(__name__)


console = Console()

# -- Constants -----------------------------------------------------------------

_PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent.parent
DEFAULT_GGUF_MODELS_DIR: Path = Path(DEFAULT_MODELS_DIR)
DEFAULT_NATIVE_MODELS_PATH: Path = Path(DEFAULT_NATIVE_MODELS_DIR)
DEFAULT_CONFIG_PATH: Path = get_user_dir() / "config.yaml"
DEFAULT_CONTEXT_LENGTH = 4096

# Common locations where users may have existing GGUF or native model assets
_COMMON_MODEL_DIRS = [
    _PROJECT_ROOT / "models",
    _PROJECT_ROOT / "models" / "native",
    Path.home() / ".cache" / "huggingface",
    Path.home() / "models",
    Path.home() / ".local" / "share" / "vetinari" / "models",
]

WIZARD_STEPS = 6  # Total steps shown to the user
_FALSE_ENV_VALUES = {"", "0", "false", "no", "off"}
_LLAMA_CPP_POLICY_USE_CASES = [
    "explicit_user_preference",
    "weak_or_no_server_setup",
    "gguf_only_models",
    "cpu_ram_vram_offload",
    "oversized_local_models",
    "recovery_fallback",
]


@dataclass
class WizardResult:
    """Outcome of the setup wizard run.

    Attributes:
        success: Whether the wizard completed successfully.
        hardware: Detected hardware profile.
        models_found: List of discovered model asset paths found on disk.
        model_downloaded: Path to downloaded model, if any.
        config_path: Path to the generated config file.
        errors: List of non-fatal errors encountered.
    """

    success: bool = False
    hardware: HardwareProfile | None = None
    models_found: list[Path] = field(default_factory=list)
    model_downloaded: Path | None = None
    config_path: Path | None = None
    vllm_setup: dict[str, Any] = field(default_factory=dict)
    nim_setup: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def __repr__(self) -> str:
        return "WizardResult(...)"


# -- Helpers ------------------------------------------------------------------


def _coerce_env_bool(value: str | None, *, default: bool = False) -> bool:
    """Coerce setup environment flags into booleans."""
    if value is None:
        return default
    return value.strip().lower() not in _FALSE_ENV_VALUES


def _normalize_backend_name(value: str | None) -> str:
    """Normalize configured backend names to setup/runtime backend ids."""
    normalized = (value or "").strip().lower().replace("-", "_")
    if normalized in {"local", "llama", "llamacpp", "llama_cpp"}:
        return "llama_cpp"
    return normalized


def _preferred_backend_from_env() -> str | None:
    """Return explicit user backend preference, if configured."""
    for env_name in ("VETINARI_PREFERRED_BACKEND", "VETINARI_INFERENCE_BACKEND"):
        preferred = _normalize_backend_name(os.environ.get(env_name))
        if preferred:
            return preferred
    return None


def _supports_vllm_setup(hardware: HardwareProfile | None) -> bool:
    """Return True when vLLM is a reasonable first-run backend candidate."""
    if hardware is None:
        return True
    if not hardware.has_gpu:
        return False
    return hardware.gpu_vendor in {GpuVendor.NVIDIA, GpuVendor.AMD, GpuVendor.INTEL}


def _supports_nim_setup(hardware: HardwareProfile | None) -> bool:
    """Return True when NIM is a reasonable first-run backend candidate."""
    if hardware is None:
        return True
    return hardware.gpu_vendor == GpuVendor.NVIDIA and hardware.cuda_available


def _select_backend_order(
    hardware: HardwareProfile,
    available_backends: list[str] | None = None,
    *,
    preferred_backend: str | None = None,
) -> list[str]:
    """Return hardware-aware backend order for generated setup config."""
    detected = {_normalize_backend_name(backend) for backend in (available_backends or ["llama_cpp"])}
    detected.discard("")
    detected.add("llama_cpp")

    order: list[str] = []

    def add(name: str) -> None:
        """Append a backend to the setup order once."""
        if name in detected and name not in order:
            order.append(name)

    preferred = _normalize_backend_name(preferred_backend) or _preferred_backend_from_env()
    if preferred:
        add(preferred)

    if _supports_nim_setup(hardware):
        add("nim")
    if _supports_vllm_setup(hardware):
        add("vllm")
    add("llama_cpp")
    return order


def _detect_available_backends(hardware: HardwareProfile | None = None) -> list[str]:
    """Detect which inference backends are available on this system.

    Checks reachability/configuration of supported local and server backends.
    llama-cpp-python is always included as the default backend.

    Returns:
        List of available backend names (e.g. ``["llama_cpp", "vllm"]``).
    """
    backends = ["llama_cpp"]  # Always available for GGUF/offload/recovery fallback.

    def add_backend(name: str) -> None:
        if name not in backends:
            backends.append(name)

    def endpoint_ready(url: str, path: str = "/v1/models") -> bool:
        try:
            import httpx

            resp = httpx.get(f"{url.rstrip('/')}{path}", timeout=5)
            return resp.status_code == 200
        except Exception:
            logger.warning("Endpoint probe failed for %s%s", url.rstrip("/"), path, exc_info=True)
            return False

    # Check for a running vLLM server. Importability alone is not a live backend.
    vllm_endpoint = os.environ.get("VETINARI_VLLM_ENDPOINT", "http://localhost:8000")
    if hardware is not None or os.environ.get("VETINARI_VLLM_ENDPOINT"):
        if is_vllm_endpoint_ready(vllm_endpoint):
            add_backend("vllm")
            logger.info("vLLM detected at %s", vllm_endpoint)
        else:
            logger.warning("vLLM endpoint %s not reachable - vLLM backend will not be available", vllm_endpoint)

    server_backend_envs = {
        "sglang": (
            "VETINARI_SGLANG_ENDPOINT",
            os.environ.get("VETINARI_SGLANG_ENDPOINT", "http://localhost:30000"),
            "/v1/models",
        ),
        "comfyui": (
            "VETINARI_COMFYUI_ENDPOINT",
            os.environ.get("VETINARI_COMFYUI_ENDPOINT", "http://localhost:8188"),
            "/system_stats",
        ),
    }
    for backend, (env_name, default_url, probe_path) in server_backend_envs.items():
        configured = os.environ.get(env_name)
        if not configured:
            continue
        endpoint = configured or default_url
        if probe_path and not endpoint_ready(endpoint, probe_path):
            logger.warning("%s endpoint %s not reachable - backend will not be available", backend, endpoint)
            continue
        add_backend(backend)
        logger.info("%s backend configured at %s", backend, endpoint)

    if os.environ.get("VETINARI_FASTER_WHISPER_MODEL"):
        add_backend("faster_whisper")
    else:
        try:
            import_module("faster_whisper")
            add_backend("faster_whisper")
        except Exception:
            logger.warning("faster-whisper package not importable - backend will not be available", exc_info=True)

    return backends


def _write_config(
    hardware: HardwareProfile,
    model_path: Path | None = None,
    config_path: Path | None = None,
    available_backends: list[str] | None = None,
    vllm_setup: VLLMContainerPlan | None = None,
    nim_setup: NIMContainerPlan | None = None,
) -> Path:
    """Write a vetinari config YAML with detected hardware settings.

    Args:
        hardware: Detected hardware profile for smart defaults.
        model_path: Path to the selected/downloaded model file.
        config_path: Where to write the config (default: ~/.vetinari/config.yaml).
        available_backends: List of detected backends (e.g. ``["llama_cpp", "vllm"]``).
        vllm_setup: Optional vLLM container setup plan to record in config.
        nim_setup: Optional NIM container setup plan to record in config.

    Returns:
        Path to the written config file.
    """
    yaml = import_module("yaml")

    config_path = config_path or (get_user_dir() / "config.yaml")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    backends = available_backends or ["llama_cpp"]

    # Smart defaults based on hardware
    n_gpu_layers = -1 if hardware.has_gpu else 0  # -1 = offload all layers to GPU
    n_ctx = DEFAULT_CONTEXT_LENGTH
    flash_attn = hardware.cuda_available  # Flash attention only with CUDA
    n_threads = max(1, hardware.cpu_count // 2)  # Half of logical cores
    n_batch = 512  # Default batch size

    backend_order = _select_backend_order(hardware, backends)
    primary_backend = backend_order[0]
    fallback_backend = backend_order[1] if len(backend_order) > 1 else "llama_cpp"

    config: dict[str, Any] = {
        "inference": {
            "n_gpu_layers": n_gpu_layers,
            "n_ctx": n_ctx,
            "n_batch": n_batch,
            "flash_attn": flash_attn,
            "n_threads": n_threads,
        },
        "local_inference": {
            "models_dir": str(model_path.parent if model_path else DEFAULT_GGUF_MODELS_DIR),
            "gpu_layers": n_gpu_layers,
            "context_length": DEFAULT_CONTEXT_LENGTH,
        },
        "models": {
            "gguf_dir": str(model_path.parent if model_path else DEFAULT_GGUF_MODELS_DIR),
            "native_dir": os.environ.get("VETINARI_NATIVE_MODELS_DIR", str(DEFAULT_NATIVE_MODELS_PATH)),
        },
        "inference_backend": {
            "selection_policy": "hardware_aware",
            "primary": primary_backend,
            "fallback": fallback_backend,
            "fallback_order": backend_order,
            "llama_cpp_use_cases": list(_LLAMA_CPP_POLICY_USE_CASES),
            "native_models_dir": os.environ.get("VETINARI_NATIVE_MODELS_DIR", str(DEFAULT_NATIVE_MODELS_PATH)),
        },
        "hardware": hardware.to_dict(),
    }

    if model_path and model_path.exists():
        config["models"] = {
            "default_model": str(model_path),
            "gguf_dir": str(model_path.parent),
            "native_dir": os.environ.get("VETINARI_NATIVE_MODELS_DIR", str(DEFAULT_NATIVE_MODELS_PATH)),
        }

    _configure_native_backends(config, backends, vllm_setup, nim_setup)

    rendered = yaml.safe_dump(config, default_flow_style=False, sort_keys=False)
    temp_path = config_path.with_name(f".{config_path.name}.tmp")
    temp_path.write_text(rendered, encoding="utf-8")
    temp_path.replace(config_path)
    return config_path


def _configure_native_backends(
    config: dict[str, Any],
    backends: list[str],
    vllm_setup: VLLMContainerPlan | None,
    nim_setup: NIMContainerPlan | None,
) -> None:
    """Add native backend details to generated config."""
    vllm_prefix_caching = os.environ.get("VETINARI_VLLM_PREFIX_CACHING_ENABLED", "true")
    config["inference_backend"]["vllm"] = {
        "enabled": "vllm" in backends,
        "endpoint": os.environ.get("VETINARI_VLLM_ENDPOINT", "http://localhost:8000"),
        "gpu_only": True,
        "semantic_cache_enabled": True,
        "cache_namespace": "vetinari",
        "cache_salt": os.environ.get("VETINARI_VLLM_CACHE_SALT", ""),
        "prefix_caching_enabled": vllm_prefix_caching.strip().lower() not in ("", "0", "false", "no", "off"),
        "prefix_caching_hash_algo": os.environ.get("VETINARI_VLLM_PREFIX_CACHING_HASH_ALGO", "sha256"),
        "container_setup": vllm_setup.to_config() if vllm_setup else {},
    }
    config["inference_backend"]["nim"] = {
        "enabled": "nim" in backends,
        "endpoint": os.environ.get("VETINARI_NIM_ENDPOINT", "http://127.0.0.1:8001"),
        "kv_cache_reuse_enabled": "nim" in backends,
        "container_setup": nim_setup.to_config() if nim_setup else {},
    }


def _smoke_test() -> bool:
    """Run a quick import test to verify the vetinari package is functional.

    Returns:
        True if the package imports successfully.
    """
    try:
        import_module("vetinari")
        return True
    except Exception as exc:
        logger.warning("Smoke test failed -- vetinari import error: %s", exc)
        return False


# -- Main Wizard --------------------------------------------------------------


def run_wizard(
    skip_download: bool = False,
    non_interactive: bool = False,
    config_path: Path | None = None,
) -> WizardResult:
    """Run the first-time setup wizard.

    Args:
        skip_download: Skip download value consumed by run_wizard().
        non_interactive: Non interactive value consumed by run_wizard().
        config_path: Path to the configuration file to load.

    Returns:
        Value produced for the caller.
    """
    result = WizardResult()
    recovery_state = load_wizard_state(config_path or DEFAULT_CONFIG_PATH)
    if recovery_state is not None and recovery_state.status in {"running", "failed"}:
        result.errors.append(
            f"Recovered previous init wizard state: {recovery_state.step} {recovery_state.status}"
            + (f" ({recovery_state.detail})" if recovery_state.detail else "")
        )

    save_wizard_state(config_path or DEFAULT_CONFIG_PATH, step="detect-hardware", status="running")
    hardware = _wizard_detect_hardware(result)
    save_wizard_state(config_path or DEFAULT_CONFIG_PATH, step="plan-backends", status="running")
    vllm_setup, nim_setup = _wizard_plan_container_backends(hardware, result)
    save_wizard_state(config_path or DEFAULT_CONFIG_PATH, step="scan-models", status="running")
    models_found, detected_backends = _wizard_scan_models_and_backends(hardware, result, vllm_setup)
    save_wizard_state(config_path or DEFAULT_CONFIG_PATH, step="recommend-models", status="running")
    recommendations = _wizard_recommend_models(hardware)
    save_wizard_state(config_path or DEFAULT_CONFIG_PATH, step="download-model", status="running")
    selected_model_path = _wizard_select_model_download(
        skip_download, non_interactive, models_found, recommendations, result
    )
    if selected_model_path is None and not skip_download and not models_found:
        detail = result.errors[-1] if result.errors else "model download did not produce a file"
        save_wizard_state(config_path or DEFAULT_CONFIG_PATH, step="download-model", status="failed", detail=detail)
    else:
        save_wizard_state(config_path or DEFAULT_CONFIG_PATH, step="download-model", status="passed")
    save_wizard_state(config_path or DEFAULT_CONFIG_PATH, step="write-config", status="running")
    _wizard_write_config(hardware, selected_model_path, config_path, detected_backends, vllm_setup, nim_setup, result)
    save_wizard_state(config_path or DEFAULT_CONFIG_PATH, step="smoke-test", status="running")
    _wizard_run_smoke_and_summary(result)
    if result.success:
        clear_wizard_state(config_path or DEFAULT_CONFIG_PATH)
    else:
        detail = result.errors[-1] if result.errors else "setup completed without success"
        save_wizard_state(config_path or DEFAULT_CONFIG_PATH, step="smoke-test", status="failed", detail=detail)
    return result


def _wizard_detect_hardware(result: WizardResult) -> HardwareProfile:
    """Run wizard hardware detection and print the summary."""
    console.print(f"\n[1/{WIZARD_STEPS}] Detecting hardware...")
    hardware = detect_hardware()
    result.hardware = hardware
    console.print(f"      CPU cores : {hardware.cpu_count}")
    console.print(f"      RAM       : {hardware.ram_gb:.1f} GB")
    if hardware.has_gpu:
        console.print(f"      GPU       : {hardware.gpu_name}")
        console.print(f"      VRAM      : {hardware.vram_gb:.1f} GB")
        vendor_info = []
        if hardware.cuda_available:
            vendor_info.append("CUDA")
        if hardware.metal_available:
            vendor_info.append("Metal")
        if vendor_info:
            console.print(f"      Accel     : {', '.join(vendor_info)}")
    else:
        console.print("      GPU       : not detected (CPU inference will be used)")
        console.print("      CPU inference fallback active; run vetinari doctor --json for diagnostics.")
    return hardware


def _wizard_plan_container_backends(
    hardware: HardwareProfile,
    result: WizardResult,
) -> tuple[VLLMContainerPlan, NIMContainerPlan]:
    """Plan optional native container setup and record wizard state."""
    vllm_setup = plan_vllm_container_setup(hardware)
    if vllm_setup.can_auto_start:
        console.print("      vLLM setup: auto mode requested; starting container...")
        vllm_setup = start_vllm_container(vllm_setup)
        _record_container_start("vLLM", vllm_setup.started, vllm_setup.error, result)
    elif vllm_setup.status in {"guided_ready", "manual_required", "missing_prerequisites", "unsupported_hardware"}:
        logger.info("vLLM container setup status: %s", vllm_setup.status)
    result.vllm_setup = vllm_setup.to_config()

    nim_setup = plan_nim_container_setup(hardware)
    if nim_setup.can_auto_start:
        console.print("      NIM setup: auto mode requested; starting container...")
        nim_setup = start_nim_container(nim_setup)
        _record_container_start("NIM", nim_setup.started, nim_setup.error, result)
    elif nim_setup.status in {"guided_ready", "manual_required", "missing_prerequisites", "unsupported_hardware"}:
        logger.info("NIM container setup status: %s", nim_setup.status)
    result.nim_setup = nim_setup.to_config()
    return vllm_setup, nim_setup


def _record_container_start(name: str, started: bool, error: str, result: WizardResult) -> None:
    """Print and record a container auto-start result."""
    if started:
        console.print(f"      {name} setup: container start requested successfully")
    else:
        result.errors.append(
            f"{name} container start failed: {error}. Config path: {DEFAULT_CONFIG_PATH}. "
            "Run vetinari doctor --json for structured diagnostics."
        )
        console.print(f"      {name} setup: container start failed; see config for details")


def plan_nim_container_setup(hardware: HardwareProfile) -> NIMContainerPlan:
    """Lazily plan NIM setup without importing the NIM module at wizard import time.

    Returns:
        Planned NIM container setup details.
    """
    _plan_nim_container_setup = import_module("vetinari.setup.nim_container").plan_nim_container_setup
    return cast("NIMContainerPlan", _plan_nim_container_setup(hardware))


def start_nim_container(plan: NIMContainerPlan) -> NIMContainerPlan:
    """Lazily start NIM setup without importing the NIM module at wizard import time.

    Returns:
        Updated NIM container plan after the start attempt.
    """
    _start_nim_container = import_module("vetinari.setup.nim_container").start_nim_container
    return cast("NIMContainerPlan", _start_nim_container(plan))


def _wizard_scan_models_and_backends(
    hardware: HardwareProfile,
    result: WizardResult,
    vllm_setup: VLLMContainerPlan,
) -> tuple[list[Path], list[str]]:
    """Scan local models, detect backends, and print wizard step 2."""
    console.print(f"\n[2/{WIZARD_STEPS}] Scanning for existing models and backends...")
    models_found = _scan_for_models()
    result.models_found = models_found
    detected_backends = _detect_available_backends(hardware)
    _print_models_found(models_found)
    console.print(f"      Backends   : {', '.join(detected_backends)}")
    _print_backend_detection(detected_backends, vllm_setup)
    return models_found, detected_backends


def _print_models_found(models_found: list[Path]) -> None:
    """Print existing model scan results."""
    if not models_found:
        console.print("      No existing models found.")
        return
    console.print(f"      Found {len(models_found)} model file(s):")
    for model_path in models_found[:10]:
        size_mb = model_path.stat().st_size / (1024 * 1024) if model_path.exists() else 0
        console.print(f"        - {model_path.name} ({size_mb:.0f} MB)")
    if len(models_found) > 10:
        console.print(f"        ... and {len(models_found) - 10} more")


def _print_backend_detection(
    detected_backends: list[str],
    vllm_setup: VLLMContainerPlan,
) -> None:
    """Print vLLM detection status."""
    if "vllm" in detected_backends:
        console.print("      vLLM       : detected (GPU-only, high throughput)")
    elif vllm_setup.hardware_eligible:
        console.print(f"      vLLM       : {vllm_setup.status.replace('_', ' ')}")


def _wizard_recommend_models(hardware: HardwareProfile) -> list[Any]:
    """Print portfolio recommendations and return flattened recommendations."""
    console.print(f"\n[3/{WIZARD_STEPS}] Recommending models for your hardware...")
    recommender = ModelRecommender()
    console.print(f"      Hardware tier: {recommender.get_tier_label(hardware)}")
    portfolio = recommender.recommend_portfolio(hardware, None)
    labels = {
        "grunt": "Classification & Routing (fast, small)",
        "worker": "Coding & Review (main workhorse)",
        "thinker": "Reasoning & Planning (complex tasks)",
    }
    for role in ("grunt", "worker", "thinker"):
        _print_role_recommendations(labels.get(role, role), portfolio.get(role, []), hardware)
    return recommender.recommend_models_multi_format(hardware, None)


def _print_role_recommendations(label: str, recommendations: list[Any], hardware: HardwareProfile) -> None:
    """Print one portfolio role's recommendations."""
    if not recommendations:
        return
    console.print(f"\n      {label}:")
    for rec in recommendations:
        marker = " (recommended)" if rec.is_primary else ""
        backend_tag = f" [{rec.backend}]" if rec.backend != "llama_cpp" else ""
        format_tag = f" ({rec.model_format.upper()})" if rec.model_format != "gguf" else ""
        offload_tag = " [CPU offload]" if not rec.gpu_only and rec.size_gb > (hardware.effective_vram_gb or 0) else ""
        console.print(f"        - {rec.name}{format_tag}{backend_tag}{offload_tag} ({rec.size_gb:.1f} GB){marker}")
        console.print(f"          {rec.reason}")
        if rec.best_for:
            console.print(f"          Best for: {', '.join(rec.best_for)}")


def _wizard_select_model_download(
    skip_download: bool,
    non_interactive: bool,
    models_found: list[Path],
    recommendations: list[Any],
    result: WizardResult,
) -> Path | None:
    console.print(f"\n[4/{WIZARD_STEPS}] Model download...")
    if skip_download:
        console.print("      Skipping download (--skip-download).")
        return None
    if models_found:
        console.print("      Using existing model(s) - download not needed.")
        return models_found[0]
    if non_interactive:
        return _download_recommended_model(recommendations, result)
    choice = input("\n      Download a model? [Y/n] ").strip().lower()
    return _download_recommended_model(recommendations, result) if choice in ("", "y", "yes") else None


def _download_recommended_model(recommendations: list[Any], result: WizardResult) -> Path | None:
    primary = next((rec for rec in recommendations if rec.is_primary), recommendations[0])
    console.print(f"      Auto-selecting: {primary.name}")
    selected_model_path = _download_model(primary)
    if selected_model_path:
        result.model_downloaded = selected_model_path
    else:
        result.errors.append(
            f"Download failed for {primary.name}. For gated Hugging Face models, set HF_TOKEN and rerun "
            "vetinari doctor --json before retrying."
        )
        console.print("      Download failed - you can download manually later.")
    return selected_model_path


def _wizard_write_config(
    hardware: HardwareProfile,
    selected_model_path: Path | None,
    config_path: Path | None,
    detected_backends: list[str],
    vllm_setup: VLLMContainerPlan,
    nim_setup: NIMContainerPlan,
    result: WizardResult,
) -> None:
    console.print(f"\n[5/{WIZARD_STEPS}] Writing configuration...")
    try:
        cfg_path = _write_config(hardware, selected_model_path, config_path, detected_backends, vllm_setup, nim_setup)
        result.config_path = cfg_path
        console.print(f"      Config written to: {cfg_path}")
    except Exception as exc:
        result.errors.append(f"Config write failed: {exc}")
        console.print(f"      Config write failed: {exc}")


def _wizard_run_smoke_and_summary(result: WizardResult) -> None:
    console.print(f"\n[6/{WIZARD_STEPS}] Running smoke test...")
    if _smoke_test():
        console.print("      Vetinari package imports successfully.")
        result.success = True
    else:
        result.errors.append("Smoke test failed - vetinari import error")
        console.print("      Smoke test failed - check installation.")
    console.print("\n" + "=" * 50)
    if result.success:
        console.print("  Setup complete! Run 'vetinari serve' to start.")
    else:
        console.print("  Setup completed with warnings:")
        for err in result.errors:
            console.print(f"    - {err}")
    console.print("=" * 50)
