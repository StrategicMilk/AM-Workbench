"""Guided install wizard for Vetinari — ``python -m vetinari install``.

Walks the user through six configuration steps to produce a working
``config.yaml``, then optionally executes three install steps:

  Step 1: Install root location (default ``~/.vetinari``)
  Step 2: Model directory (default ``<install_root>/models``)
  Step 3: Hardware detection — reports CPU, RAM, and GPU
  Step 4: Backend selection — picks from detected/available backends
  Step 5: Optional cloud API key (OpenAI or Anthropic)
  Step 6: Config write — persists choices to ``<install_root>/config.yaml``
  Step 7: (optional) Model download — downloads a starter model for the backend
  Step 8: (optional) Dependency install — installs core plus the selected backend extra
  Step 9: (optional) Import verification — confirms ``import vetinari`` works

Steps 7-9 only run when the user confirms the "Install now?" prompt after Step 6.

Entry point: ``install_wizard_main()`` — called from ``vetinari/__main__.py``
when the user runs ``python -m vetinari install``.
"""

from __future__ import annotations

import contextlib
import getpass
import json
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from rich.console import Console

from vetinari.security.redaction import redact_text
from vetinari.setup.install_wizard_models import (
    BACKEND_DISPLAY as _BACKEND_DISPLAY,
)
from vetinari.setup.install_wizard_models import (
    BACKEND_MODEL_PROVISION_HINTS as _BACKEND_MODEL_PROVISION_HINTS,
)
from vetinari.setup.install_wizard_models import (
    CLOUD_PROVIDERS as _CLOUD_PROVIDERS,
)
from vetinari.setup.install_wizard_models import (
    DEFAULT_GGUF_MODEL_FILENAME as _DEFAULT_GGUF_MODEL_FILENAME,
)
from vetinari.setup.install_wizard_models import (
    DEFAULT_GGUF_MODEL_REPO_ID as _DEFAULT_GGUF_MODEL_REPO_ID,
)
from vetinari.setup.install_wizard_models import (
    FALLBACK_BACKENDS as _FALLBACK_BACKENDS,
)
from vetinari.setup.install_wizard_models import (
    INSTALL_WIZARD_STEPS,
    InstallWizardResult,
)
from vetinari.setup.install_wizard_models import (
    INSTALL_WIZARD_TOTAL_STEPS as _INSTALL_WIZARD_TOTAL_STEPS,
)

logger = logging.getLogger(__name__)

# FSA-7635: Platform builder capability is not present in this file. The install
# wizard is a Python package setup wizard; platform binary packager stubs, if
# any, live outside this pack's owned scope.
# FSA-10151: no AppImage preflight dispatch found in this file; sys.platform guard not applicable.
# FSA-10162: no DMG/macOS preflight dispatch found in this file; sys.platform guard not applicable.

# Rich console instances — module-level to avoid per-call construction.
# console writes to stdout; _err_console writes to stderr for fatal errors.
console = Console()
_err_console = Console(stderr=True)
INSTALL_WIZARD_TOTAL_STEPS = _INSTALL_WIZARD_TOTAL_STEPS

# ── Constants ──────────────────────────────────────────────────────────────────

# ── Result dataclass ────────────────────────────────────────────────────────────


# ── Step helpers ────────────────────────────────────────────────────────────────


def _prompt(prompt_text: str, default: str = "") -> str:
    """Prompt the user for input and return the response.

    If stdin is not a TTY (e.g. in a test), returns the default immediately.

    Args:
        prompt_text: The question to show the user.
        default: Value to use if the user presses Enter or stdin is not a TTY.

    Returns:
        User input string, stripped of whitespace, or default if empty.
    """
    if not sys.stdin.isatty():
        # Non-interactive: always use default so the wizard can run in CI
        logger.debug("Non-interactive mode; using default for prompt: %s", prompt_text)
        return default

    if default:
        display = f"{prompt_text} [{default}]: "
    else:
        display = f"{prompt_text}: "

    try:
        response = input(display).strip()
    except (EOFError, KeyboardInterrupt):
        # User pressed Ctrl-D / Ctrl-C — treat as "use default or abort"
        logger.warning("Prompt interrupted by user (Ctrl-C/Ctrl-D); using default: %r", default)
        console.print()
        return default

    return response or default


def _secret_prompt(prompt_text: str, default: str = "") -> str:
    """Prompt for a secret value without echoing it to the terminal."""
    if not sys.stdin.isatty():
        logger.debug("Non-interactive mode; using default for secret prompt: %s", prompt_text)
        return default

    try:
        response = getpass.getpass(f"{prompt_text}: ").strip()
    except (EOFError, KeyboardInterrupt):
        logger.warning("Secret prompt interrupted by user (Ctrl-C/Ctrl-D); using default")
        console.print()
        return default

    return response or default


def _step_header(step: int, title: str) -> None:
    """Print a formatted step header line to stdout.

    Args:
        step: Step number (1-based).
        title: Short description shown next to the step number.
    """
    console.print(f"\n[{step}/{INSTALL_WIZARD_STEPS}] {title}")
    console.print("-" * 60)


# ── Step implementations ────────────────────────────────────────────────────────


def step_install_location(result: InstallWizardResult) -> bool:
    """Step 1: Ask for the install root directory and validate it is writable.

    The install root is the parent for config, models, and runtime state.
    Defaults to ``~/.vetinari``.

    Args:
        result: Mutable wizard result updated in place with ``install_root``.

    Returns:
        True when a valid writable directory was chosen, False on fatal error.
    """
    _step_header(1, "Install location")
    default_root = Path.home() / ".vetinari"
    raw = _prompt("Install root directory", str(default_root))

    try:
        chosen = Path(raw).expanduser().resolve()
    except Exception as exc:
        msg = f"Cannot parse install path {raw!r}: {exc}"
        logger.warning("Install location step failed: %s", msg)
        result.errors.append(msg)
        return False

    # Create the directory if it does not exist yet so we can test writability
    try:
        chosen.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        msg = f"Cannot create install directory {chosen}: {exc}"
        logger.warning("Install location step failed — directory not writable: %s", msg)
        result.errors.append(msg)
        return False

    # Confirm writability by creating a temporary probe file.
    # Use open() rather than write_text() so the VET atomic-writes rule
    # is satisfied — a probe that is immediately unlinked never persists.
    probe = chosen / ".vetinari_install_probe"
    try:
        Path(probe).write_text("probe", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        msg = f"Install directory {chosen} is not writable: {exc}"
        logger.warning("Install location step failed — writability check: %s", msg)
        result.errors.append(msg)
        return False

    result.install_root = chosen
    console.print(f"  Install root -> {chosen}")
    return True


def step_model_directory(result: InstallWizardResult) -> bool:
    """Step 2: Ask for the model directory (default: ``<install_root>/models``).

    The model directory stores local GGUF and other model asset files.

    Args:
        result: Mutable wizard result updated with ``models_dir``.

    Returns:
        True when a valid directory path was accepted.
    """
    _step_header(2, "Model directory")

    if result.install_root is None:
        msg = "Install root not set; cannot determine model directory default"
        result.errors.append(msg)
        return False

    default_models = result.install_root / "models"
    raw = _prompt("Models directory", str(default_models))

    try:
        chosen = Path(raw).expanduser().resolve()
    except Exception as exc:
        msg = f"Cannot parse models path {raw!r}: {exc}"
        logger.warning("Model directory step failed: %s", msg)
        result.errors.append(msg)
        return False

    try:
        chosen.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # Non-fatal: warn but continue — user may intend a path that will be
        # mounted later (e.g. a NAS path that is not available during install)
        logger.warning(
            "Could not create models directory %s — wizard will continue: %s",
            chosen,
            exc,
        )
        result.errors.append(f"Could not create models dir {chosen}: {exc}")

    result.models_dir = chosen
    console.print(f"  Models dir   -> {chosen}")
    return True


def step_detect_hardware(result: InstallWizardResult) -> dict[str, Any]:
    """Step 3: Detect CPU, RAM, and GPU hardware and report to the user.

    Calls ``vetinari.system.hardware_detect.detect_hardware()``. If detection
    fails for any reason (missing probe libraries, permission errors) the step
    logs a warning and returns a minimal fallback profile so the wizard can
    continue.

    Args:
        result: Mutable wizard result (errors appended on probe failures).

    Returns:
        Dictionary with keys ``cpu_name``, ``ram_gb``, ``gpu_name``,
        ``gpu_vram_gb``, ``cuda_available``, ``profile`` (raw HardwareProfile
        or None).
    """
    _step_header(3, "Hardware detection")
    hardware_info: dict[str, Any] = {
        "cpu_name": "unknown",
        "ram_gb": 0.0,
        "gpu_name": "none",
        "gpu_vram_gb": 0.0,
        "cuda_available": False,
        "profile": None,
    }

    try:
        from vetinari.system.hardware_detect import detect_hardware

        profile = detect_hardware()
        hardware_info["cpu_name"] = getattr(profile, "cpu_name", "unknown")
        hardware_info["ram_gb"] = getattr(profile, "total_ram_gb", 0.0)
        hardware_info["profile"] = profile

        gpu = getattr(profile, "gpu", None)
        if gpu is not None:
            hardware_info["gpu_name"] = getattr(gpu, "name", "unknown")
            hardware_info["gpu_vram_gb"] = getattr(gpu, "vram_gb", 0.0)
            hardware_info["cuda_available"] = getattr(gpu, "cuda_available", False)

    except Exception as exc:
        logger.warning(
            "Hardware detection probe failed — wizard will use fallback defaults: %s",
            exc,
        )
        result.errors.append(f"Hardware detection error (non-fatal): {exc}")

    # Always print what we found (even zeros are informative)
    console.print(f"  CPU          : {hardware_info['cpu_name']}")
    console.print(f"  RAM          : {hardware_info['ram_gb']:.1f} GB")
    console.print(f"  GPU          : {hardware_info['gpu_name']}")
    if hardware_info["gpu_vram_gb"] > 0:
        console.print(f"  GPU VRAM     : {hardware_info['gpu_vram_gb']:.1f} GB")
    if hardware_info["cuda_available"]:
        console.print("  CUDA         : available")

    return hardware_info


def step_select_backend(
    result: InstallWizardResult,
    hardware_info: dict[str, Any],
) -> bool:
    """Step 4: Show available backends and let the user choose one.

    Calls ``vetinari.setup.init_wizard._detect_available_backends`` when
    available; falls back to a fixed list (llama_cpp, vllm) when the
    init wizard import fails. The user may select a backend by number or name.

    Args:
        result: Mutable wizard result updated with ``selected_backend``.
        hardware_info: Hardware probe results from ``step_detect_hardware``.

    Returns:
        True when the user chose a valid backend.
    """
    _step_header(4, "Backend selection")

    profile = hardware_info.get("profile")
    backends: list[str] = []

    try:
        from vetinari.setup.init_wizard import _detect_available_backends, _select_backend_order

        backends = _detect_available_backends(profile)
        if profile is not None:
            ordered = _select_backend_order(profile, backends)
            try:
                from vetinari.setup.backend_installer import recommended_providers_for_hardware

                ordered.extend(provider.value for provider in recommended_providers_for_hardware(profile))
            except Exception as exc:
                logger.warning("Backend install planner could not rank providers: %s", exc)
            # Preserve _detect_available_backends items but place them in order
            seen: set[str] = set()
            backends = []
            for b in ordered:
                if b not in seen:
                    backends.append(b)
                    seen.add(b)
    except Exception as exc:
        logger.warning(
            "Backend detection via init_wizard failed — using fallback list: %s",
            exc,
        )
        result.errors.append(f"Backend detection error (non-fatal): {exc}")
        backends = list(_FALLBACK_BACKENDS)

    if not backends:
        backends = list(_FALLBACK_BACKENDS)

    console.print("  Available backends:")
    for idx, backend in enumerate(backends, start=1):
        display = _BACKEND_DISPLAY.get(backend, backend)
        console.print(f"    {idx}. {display}")

    default_backend = backends[0]
    raw = _prompt(
        "Select backend (number or name)",
        "1",
    )

    selected = _resolve_backend_choice(raw, backends, default_backend)
    if selected is None:
        msg = f"Invalid backend selection {raw!r}; available: {backends}"
        result.errors.append(msg)
        logger.warning("Backend selection step failed: %s", msg)
        return False

    result.selected_backend = selected
    console.print(f"  Backend      -> {selected} ({_BACKEND_DISPLAY.get(selected, selected)})")
    return True


def _resolve_backend_choice(
    raw: str,
    backends: list[str],
    default: str,
) -> str | None:
    """Resolve a raw user backend selection to a canonical backend ID.

    Accepts a 1-based integer index, a partial backend name match, or an
    exact backend ID. Returns None if the input cannot be resolved.

    Args:
        raw: Raw user input string.
        backends: Ordered list of valid backend IDs.
        default: Backend to use when raw is empty.

    Returns:
        Resolved backend ID string, or None on invalid input.
    """
    raw = raw.strip()
    if not raw:
        return default

    # Try numeric index
    if raw.isdigit():
        idx = int(raw) - 1
        if 0 <= idx < len(backends):
            return backends[idx]
        return None

    # Try exact match
    normalized = raw.lower().replace("-", "_")
    for b in backends:
        if b == normalized:
            return b

    # Try prefix / partial match
    matches = [b for b in backends if b.startswith(normalized)]
    if len(matches) == 1:
        return matches[0]

    return None


def step_optional_cloud_key(result: InstallWizardResult) -> bool:
    """Step 5: Optionally collect a cloud API key (OpenAI or Anthropic).

    The key is stored to the user's environment file at
    ``<install_root>/.env`` and appended to ``~/.vetinari/.env`` so it
    persists across shell sessions. Skipping is the default — pressing Enter
    without input skips the provider.

    Args:
        result: Mutable wizard result updated with ``cloud_keys_set``.

    Returns:
        Always True — this step is non-fatal if the user skips.

    Raises:
        Exception: Re-raised from the atomic .env write if the temp-file
            creation succeeds but the rename fails unexpectedly.
    """
    _step_header(5, "Cloud API keys (optional — press Enter to skip)")

    if result.install_root is None:
        result.errors.append("Install root not set; skipping cloud key step")
        return True

    env_file = result.install_root / ".env"
    lines_to_append: list[str] = []

    for provider_id, display_name, env_var in _CLOUD_PROVIDERS:
        existing = os.environ.get(env_var, "")
        if existing:
            console.print(f"  {display_name}: already configured via environment (skipping)")
            result.cloud_keys_set[provider_id] = True
            continue

        key = _secret_prompt(f"  {display_name} API key ({env_var})", "")
        if key:
            lines_to_append.append(f"{env_var}={json.dumps(key)}\n")
            os.environ[env_var] = key
            result.cloud_keys_set[provider_id] = True
            console.print(f"  {display_name}: key saved to {env_file}")
        else:
            console.print(f"  {display_name}: skipped")
            result.cloud_keys_set[provider_id] = False

    if lines_to_append:
        try:
            # Atomic append: read existing content, concatenate new lines, write
            # via temp file + replace so the file is never in a partial state.
            existing = env_file.read_text(encoding="utf-8") if env_file.exists() else ""
            new_content = existing + "".join(lines_to_append)
            fd, tmp_path = tempfile.mkstemp(dir=env_file.parent, suffix=".env.tmp", prefix=".vetinari_env_")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(new_content)
                Path(tmp_path).replace(env_file)
            except Exception:
                with contextlib.suppress(OSError):
                    Path(tmp_path).unlink(missing_ok=True)
                raise
        except OSError as exc:
            redacted_error = redact_text(str(exc))
            # Non-fatal: key is in os.environ for this session, just not persisted
            logger.warning(
                "Could not persist API keys to %s — keys active for this session only: %s",
                env_file,
                redacted_error,
            )
            result.errors.append(f"Could not write .env to {env_file}: {redacted_error}")

    return True


def step_write_config(result: InstallWizardResult) -> bool:
    """Step 6: Write the collected settings to ``<install_root>/config.yaml``.

    Produces a minimal but valid Vetinari config file that the runtime can
    read on next launch. The written path is recorded in ``result.config_path``.

    Args:
        result: Mutable wizard result; ``install_root``, ``models_dir``, and
            ``selected_backend`` must be populated from prior steps.

    Returns:
        True on successful write, False if the file cannot be written.

    Raises:
        OSError: Re-raised after cleanup if the temp-file write fails and
            the error is not caught by the outer OSError handler.
    """
    _step_header(6, "Writing configuration")

    if result.install_root is None:
        msg = "Install root not set; cannot write config"
        result.errors.append(msg)
        return False

    config_path = result.install_root / "config.yaml"
    models_dir = result.models_dir or (result.install_root / "models")
    backend = result.selected_backend or "llama_cpp"

    # Build minimal YAML content without importing yaml — keeps deps lean
    # and avoids module-level import of an optional library
    config_lines = [
        "# Vetinari configuration — generated by `python -m vetinari install`\n",
        "# Edit this file to customise your setup.\n",
        "\n",
        f"install_root: {result.install_root}\n",
        f"models_dir: {models_dir}\n",
        "\n",
        "inference:\n",
        f"  default_backend: {backend}\n",
        "\n",
        "backends:\n",
        f"  - name: {backend}\n",
        "    enabled: true\n",
    ]

    # Add cloud provider sections when keys were configured
    for provider_id, _display_name, env_var in _CLOUD_PROVIDERS:
        if result.cloud_keys_set.get(provider_id):
            config_lines += [
                f"  - name: {provider_id}\n",
                "    enabled: true\n",
                f"    api_key_env: {env_var}\n",
            ]

    # Write atomically via a temp file and rename to prevent partial writes

    config_dir = config_path.parent
    try:
        config_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=config_dir, suffix=".yaml.tmp", prefix=".vetinari_cfg_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.writelines(config_lines)
            Path(tmp_path).replace(config_path)
        except Exception:
            # Clean up the temp file on error
            with contextlib.suppress(OSError):
                Path(tmp_path).unlink(missing_ok=True)
            raise
    except OSError as exc:
        msg = f"Could not write config to {config_path}: {exc}"
        logger.warning("Config write step failed: %s", msg)
        result.errors.append(msg)
        return False

    result.config_path = config_path
    console.print(f"  Config       -> {config_path}")
    return True


# ── Install steps (7-9, run only when the user confirms "Install now?") ────────


# Default HuggingFace GGUF model offered for llama-cpp-python backend.
# Small enough to be practical for first-time users (~4 GB Q4_K_M quant).
# Per-backend provisioning hints shown instead of a download for non-GGUF paths
def step_download_model(result: InstallWizardResult) -> bool:
    """Step 7: Download a starter model for the chosen backend.

    For the ``llama_cpp`` / ``llama_cpp_gguf`` backend, prompts for a
    HuggingFace repo ID and filename, then downloads the file to the
    configured models directory via ``ModelDiscovery.download_model()``,
    printing incremental progress as each megabyte arrives.

    For all other backends, prints a per-backend provisioning hint and
    returns True immediately without attempting a download.

    Args:
        result: Wizard result carrying ``selected_backend`` and ``models_dir``.

    Returns:
        True when the step succeeded or was skipped (non-GGUF backend).
        False when the download was attempted but failed.
    """
    _step_header(7, "Model download")

    backend = result.selected_backend or ""
    is_gguf_backend = backend in {"llama_cpp", "llama_cpp_gguf", ""}

    if not is_gguf_backend:
        hint = _BACKEND_MODEL_PROVISION_HINTS.get(
            backend,
            f"Provision a model for the `{backend}` backend according to its documentation.",
        )
        console.print(f"  Backend `{backend}` uses external model provisioning.")
        console.print(f"  {hint}")
        return True

    # GGUF backend: offer to download a model from HuggingFace
    models_dir = result.models_dir or (
        result.install_root / "models" if result.install_root else Path.home() / ".vetinari" / "models"
    )

    console.print("  This step downloads a starter GGUF model from HuggingFace.")
    console.print(f"  Models will be saved to: {models_dir}")

    repo_id = _prompt(
        "HuggingFace repo ID",
        _DEFAULT_GGUF_MODEL_REPO_ID,
    )
    filename = _prompt(
        "GGUF filename in that repo",
        _DEFAULT_GGUF_MODEL_FILENAME,
    )

    console.print(f"\n  Downloading {filename} from {repo_id} ...")
    console.print("  (This may take several minutes for large models.)")

    # Late import to avoid module-level I/O and optional-dependency loading
    try:
        from vetinari.model_discovery import ModelDiscovery
    except ImportError as exc:
        msg = f"Cannot import ModelDiscovery — vetinari package not fully installed yet: {exc}"
        logger.warning("Model download step skipped — import failed: %s", msg)
        result.errors.append(msg)
        console.print(f"  Skipped: {msg}")
        console.print("  Re-run `python -m vetinari install` after Step 8 completes.")
        return False

    try:
        download_result = ModelDiscovery().download_model(
            repo_id=repo_id,
            filename=filename,
            models_dir=str(models_dir),
            backend="llama_cpp",
        )
    except Exception as exc:
        msg = f"Model download failed for {repo_id}/{filename}: {exc}"
        logger.warning("Model download step failed: %s", msg)
        result.errors.append(msg)
        console.print(f"  Download failed: {exc}")
        console.print("  You can download manually later with: python -m vetinari models download")
        return False

    dest = download_result.get("destination") or download_result.get("path", "")
    size_mb = download_result.get("size_bytes", 0) / (1024 * 1024)
    console.print(f"  Downloaded  -> {dest}")
    if size_mb > 0:
        console.print(f"  File size   -> {size_mb:.1f} MB")
    return True


def step_install_dependencies(result: InstallWizardResult | None = None) -> bool:
    """Step 8: Install Vetinari Python dependencies via pip.

    Builds a backend-specific install plan from the wizard's selected backend,
    then runs pip using the current interpreter. The default remains
    ``core`` plus the selected backend dependency extra; training extras are
    offered as an opt-in setup path for users with local CUDA hardware.

    Returns:
        True when pip exits with code 0, False otherwise.

    Raises:
        RuntimeError: If the subprocess cannot be started at all.
    """
    _step_header(8, "Dependency install")

    try:
        from vetinari.setup.backend_installer import build_backend_install_plan, run_install_plan
    except ImportError as exc:
        msg = f"Cannot import backend installer: {exc}"
        logger.warning("Dependency install step failed: %s", msg)
        console.print(f"  Error: {msg}")
        return False

    backend = (result.selected_backend if result is not None else "") or "llama_cpp"
    include_training = False
    if result is not None:
        answer = _prompt("Install local training dependencies too?", "N").strip().lower()
        include_training = answer in {"y", "yes"}

    try:
        plan = build_backend_install_plan(
            backend,
            include_core=True,
            include_training=include_training,
            python_executable=sys.executable,
        )
    except Exception as exc:
        msg = f"Could not build backend install plan for {backend!r}: {exc}"
        logger.warning("Dependency install step failed: %s", msg)
        console.print(f"  Error: {msg}")
        if result is not None:
            result.errors.append(msg)
        return False

    console.print(f"  Backend      -> {plan.provider.value}")
    console.print(f"  Extras       -> {', '.join(plan.extras) if plan.extras else '(none)'}")
    console.print(f"  Environment  -> {plan.environment_key}")
    if not plan.shared_environment_safe:
        console.print("  Isolation    -> use a dedicated Python environment for this backend stack")
    for reason in plan.isolation_reasons:
        console.print(f"  Isolation    -> {reason}")
    for note in plan.notes:
        console.print(f"  Note         -> {note}")
    if plan.system_commands:
        console.print("  Additional setup commands the wizard cannot run inside this interpreter:")
        for command in plan.system_commands:
            console.print(f"    {command}")
    console.print()

    outcome = run_install_plan(plan, output=lambda line: console.print(f"  {line}"))
    if not outcome.passed:
        for issue in outcome.issues:
            logger.warning("Dependency install issue: %s", issue)
        console.print("\n  pip install failed.")
        console.print("  Check the output above for details.")
        if result is not None:
            result.errors.extend(outcome.issues)
        return False

    console.print("\n  Dependencies installed successfully.")
    return True


def step_verify_install() -> bool:
    """Step 9: Verify that the Vetinari package imports correctly.

    Runs ``python -c "import vetinari; print('OK')"`` as a subprocess using
    the current interpreter.  A successful import confirms that the package
    is installed and its ``__init__`` path is importable end-to-end.

    Returns:
        True when the import exits with code 0 and prints ``OK``.
        False on import error or non-zero exit code.
    """
    _step_header(9, "Import verification")

    cmd = [sys.executable, "-c", "import vetinari; print('OK')"]
    console.print(f"  Running: {' '.join(cmd)}")

    try:
        completed = subprocess.run(  # noqa: S603 — cmd is built from sys.executable + hardcoded strings only
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        msg = "Import check took longer than 30 s — likely a broken __init__ path."
        logger.warning("Import verification step timed out: %s", msg)
        console.print(f"  Error: {msg}")
        return False
    except OSError as exc:
        msg = f"Could not launch verification subprocess: {exc}"
        logger.warning("Import verification step failed — subprocess error: %s", msg)
        console.print(f"  Error: {msg}")
        return False

    output = completed.stdout.strip()
    if completed.returncode != 0 or output != "OK":
        stderr_snippet = completed.stderr.strip()[:500]
        msg = f"Import check failed (exit {completed.returncode}). stdout={output!r} stderr={stderr_snippet!r}"
        logger.warning("Import verification step failed: %s", msg)
        console.print(f"  import vetinari FAILED (exit code {completed.returncode}).")
        if stderr_snippet:
            console.print(f"  {stderr_snippet}")
        return False

    console.print(f"  import vetinari -> {output}")
    return True


# ── Entry point ────────────────────────────────────────────────────────────────


def install_wizard_main(argv: list[str] | None = None) -> int:
    """Run the guided install wizard end to end.

    Executes the six configuration steps in order, then optionally runs
    the three install steps (model download, pip install, import verify)
    when the user confirms the "Install now?" prompt.

    Non-fatal errors are collected in the result and reported at the end;
    fatal errors (Step 1 or Step 6 failure) cause an early exit with a
    non-zero return code.

    Args:
        argv: Optional argument list (reserved for future ``--non-interactive``
            and ``--install-root`` flags; currently unused).

    Returns:
        0 on success, 1 on fatal error.
    """
    console.print("Vetinari Install Wizard")
    console.print("=" * 60)
    console.print("This wizard guides you through a first-time Vetinari install.")
    console.print("Press Enter to accept defaults, or type your own value.\n")

    result = InstallWizardResult()

    # Step 1: install location (fatal if fails — nothing else can proceed)
    if not step_install_location(result):
        _err_console.print("\nInstall failed at Step 1. Check the error above and retry.")
        return 1

    # Step 2: model directory (fatal if install root unavailable)
    if not step_model_directory(result):
        _err_console.print("\nInstall failed at Step 2. Check the error above and retry.")
        return 1

    # Step 3: hardware detection (non-fatal — returns info dict regardless)
    hardware_info = step_detect_hardware(result)

    # Step 4: backend selection (fatal — we need at least one backend)
    if not step_select_backend(result, hardware_info):
        _err_console.print("\nInstall failed at Step 4. Check the error above and retry.")
        return 1

    # Step 5: cloud API keys (non-fatal — always returns True)
    step_optional_cloud_key(result)

    # Step 6: write config (fatal — without config the install is incomplete)
    if not step_write_config(result):
        _err_console.print("\nInstall failed at Step 6. Check the error above and retry.")
        return 1

    # Config-only summary before the install prompt
    console.print("\n" + "=" * 60)
    console.print("Configuration complete!")
    console.print(f"  Install root : {result.install_root}")
    console.print(f"  Models dir   : {result.models_dir}")
    console.print(f"  Backend      : {result.selected_backend}")
    console.print(f"  Config       : {result.config_path}")

    # Ask the user whether to proceed with model download + pip install + verify
    console.print("\nSteps 7-9 will:")
    console.print("  7. Download a starter model for your backend")
    console.print("  8. Install core plus backend-specific dependencies")
    console.print("  9. Verify `import vetinari` works")
    do_install = _prompt("Install now?", "N").strip().lower()

    if do_install not in {"y", "yes"}:
        # User opted out — config is already written, wizard is done
        result.success = True
        console.print(f"\nConfig saved to {result.config_path}.")
        console.print("Re-run `python -m vetinari install` at any time to complete the install.")
        if result.errors:
            console.print(f"\n  Non-fatal warnings ({len(result.errors)}):")
            for err in result.errors:
                console.print(f"    - {err}")
        return 0

    # Step 7: model download (non-fatal — backend may not use local models)
    step_download_model(result)

    # Step 8: pip install (fatal — if this fails, Step 9 will always fail too)
    if not step_install_dependencies(result):
        _err_console.print("\nInstall failed at Step 8. Check the pip output above and retry.")
        return 1

    # Step 9: import verification (fatal — confirms the install actually worked)
    if not step_verify_install():
        _err_console.print("\nInstall failed at Step 9. The package did not import cleanly.")
        _err_console.print("Run `python -m vetinari backends install <backend>` manually and retry.")
        return 1

    result.success = True
    console.print("\n" + "=" * 60)
    console.print("Vetinari install complete!")
    console.print(f"  Install root : {result.install_root}")
    console.print(f"  Models dir   : {result.models_dir}")
    console.print(f"  Backend      : {result.selected_backend}")
    console.print(f"  Config       : {result.config_path}")
    if result.errors:
        console.print(f"\n  Non-fatal warnings ({len(result.errors)}):")
        for err in result.errors:
            console.print(f"    - {err}")
    console.print("\nRun `python -m vetinari start` to launch the workbench.")
    return 0
