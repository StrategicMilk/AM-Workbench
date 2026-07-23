"""Local model file helpers for the packaging CLI."""

from __future__ import annotations

import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from vetinari.cli_packaging_data import _RICH_AVAILABLE, _console, _get_recommended_models
from vetinari.utils.bounded_collections import BoundedList

logger = logging.getLogger(__name__)


def _write_line(text: object = "") -> None:
    """Write one CLI output line to standard output."""
    sys.stdout.write(f"{text}\n")


_MAX_MODEL_SCAN_FILES = 5000
_MAX_MODEL_SCAN_RESULTS = 5000
_MAX_MODEL_SCAN_DEPTH = 8
_LOCAL_MODEL_SUFFIXES = {".gguf", ".safetensors"}
_NATIVE_BACKENDS = {"vllm", "nim"}
_SAFE_MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9._ -]{1,160}$")


def _guess_model_file_type(path: Path) -> str:
    identity = str(path).lower()
    if "awq" in identity:
        return "awq"
    if "gptq" in identity:
        return "gptq"
    return path.suffix.lower().lstrip(".")


# ── Rich imports (conditional on _RICH_AVAILABLE) ─────────────────────────────
# The Table class is only used inside functions that guard with
# _RICH_AVAILABLE, so NameError is impossible when rich is absent.
if _RICH_AVAILABLE:
    from rich.table import Table as _Table


# ── Model directory helpers ────────────────────────────────────────────────────


def _local_file_matches_filters(
    path: Path,
    *,
    objective: str | None = None,
    family: str | None = None,
    quantization: str | None = None,
    file_type: str | None = None,
    min_size_gb: float | None = None,
    max_size_gb: float | None = None,
) -> bool:
    name = path.name.lower()
    identity = str(path).lower()
    if objective and objective.strip().lower() not in {"", "general", "any"}:
        objective_keywords = {
            "coding": ("code", "coder", "codestral", "starcoder", "deepseek"),
            "code": ("code", "coder", "codestral", "starcoder", "deepseek"),
            "chat": ("chat", "instruct", "assistant"),
            "instruction": ("chat", "instruct"),
            "reasoning": ("reason", "math", "r1", "qwq"),
            "math": ("reason", "math", "qwq"),
            "vision": ("vision", "vl", "vqa"),
            "embeddings": ("embed", "embedding", "e5", "bge"),
        }
        keywords = objective_keywords.get(objective.strip().lower(), (objective.strip().lower(),))
        if not any(keyword in name for keyword in keywords):
            return False
    if family and family.strip().lower() not in {"", "any"} and _guess_family(path.name) != family.strip().lower():
        return False
    if quantization and quantization.strip().lower() not in {"", "any"}:
        expected = quantization.strip().upper()
        guessed = _guess_quantization(path.name).upper()
        if expected not in guessed and expected not in str(path).upper():
            return False
    if file_type and file_type.strip().lower() not in {"", "any"}:
        expected_type = file_type.strip().lower().lstrip(".")
        if expected_type in {"awq", "gptq"}:
            if expected_type not in identity:
                return False
        elif path.suffix.lower().lstrip(".") != expected_type:
            return False
    size_gb = path.stat().st_size / 1024**3
    if min_size_gb is not None and size_gb < min_size_gb:
        return False
    return not (max_size_gb is not None and size_gb > max_size_gb)


def _iter_model_files(
    models_dir: Path,
    *,
    objective: str | None = None,
    family: str | None = None,
    quantization: str | None = None,
    file_type: str | None = None,
    min_size_gb: float | None = None,
    max_size_gb: float | None = None,
) -> list[Path]:
    """Return model files under a models directory, including nested repos."""
    if not models_dir.is_dir():
        return []
    root = models_dir.resolve()
    found = BoundedList[Path](_MAX_MODEL_SCAN_RESULTS)
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        try:
            depth = len(current.relative_to(root).parts)
        except ValueError:
            logger.warning("Skipped model scan path outside root: %s", current)
            continue
        if depth >= _MAX_MODEL_SCAN_DEPTH:
            dirnames[:] = []
        else:
            dirnames[:] = [name for name in dirnames if not name.startswith(".")]
        for filename in filenames:
            scanned += 1
            if scanned > _MAX_MODEL_SCAN_FILES:
                logger.warning("Model scan stopped after %d files under %s", _MAX_MODEL_SCAN_FILES, root)
                return sorted(found)
            path = current / filename
            if path.suffix.lower() in _LOCAL_MODEL_SUFFIXES and _local_file_matches_filters(
                path,
                objective=objective,
                family=family,
                quantization=quantization,
                file_type=file_type,
                min_size_gb=min_size_gb,
                max_size_gb=max_size_gb,
            ):
                found.append(path)
    return sorted(found)


def _model_name_query(name: str | None) -> str | None:
    """Return a safe model filename query or None when the query is unsafe."""
    if not name:
        return None
    query = name.strip()
    if not query or "/" in query or "\\" in query or ".." in query or not _SAFE_MODEL_NAME_RE.fullmatch(query):
        return None
    return query.casefold()


def _find_model_candidates(name: str | None, models_dir: Path) -> list[Path] | None:
    """Find model files using the bounded scanner rather than recursive glob patterns."""
    query = _model_name_query(name)
    if query is None:
        return None
    return [path for path in _iter_model_files(models_dir) if query in path.name.casefold()]


def _find_models_dir() -> Path:
    """Return the first models directory that exists, preferring the user dir.

    Returns:
        The models directory ``Path``.  May not yet exist on disk.
    """
    from vetinari.constants import (
        DEFAULT_MODELS_DIR,
        OPERATOR_MODELS_CACHE_DIR,
    )

    operator_models = Path(OPERATOR_MODELS_CACHE_DIR)
    if operator_models.exists() and _iter_model_files(operator_models):
        return operator_models
    project_models = Path(DEFAULT_MODELS_DIR)
    if project_models.exists():
        return project_models
    if operator_models.exists():
        return operator_models
    return operator_models


def _find_native_models_dir() -> Path:
    """Return the native Hugging Face-format model root for vLLM/NIM."""
    from vetinari.constants import (
        DEFAULT_NATIVE_MODELS_DIR,
    )

    return Path(DEFAULT_NATIVE_MODELS_DIR)


def _guess_quantization(filename: str) -> str:
    """Infer the quantization level from a GGUF filename convention.

    Recognises common suffixes produced by llama.cpp's quantization tool such as
    ``Q4_K_M``, ``Q6_K``, ``F16``, ``Q8_0``, etc.

    Args:
        filename: Basename or full path string of the GGUF file.

    Returns:
        A short quantization label string, e.g. ``"Q4_K_M"`` or ``"unknown"``.
    """
    name = str(filename).upper()
    quant_markers = [
        "AWQ",
        "GPTQ",
        "Q2_K",
        "Q3_K_S",
        "Q3_K_M",
        "Q3_K_L",
        "Q4_0",
        "Q4_1",
        "Q4_K_S",
        "Q4_K_M",
        "Q5_0",
        "Q5_1",
        "Q5_K_S",
        "Q5_K_M",
        "Q6_K",
        "Q8_0",
        "F16",
        "F32",
        "BF16",
    ]
    for marker in quant_markers:
        if marker in name:
            return marker
    return "unknown"


def _guess_family(filename: str) -> str:
    """Guess the model family from a GGUF filename.

    Args:
        filename: Basename or full path string of the GGUF file.

    Returns:
        A family label such as ``"llama"``, ``"mistral"``, or ``"unknown"``.
    """
    name = Path(filename).stem.lower()
    # More-specific prefixes MUST precede their substrings so that e.g.
    # "tinyllama" matches before "llama" and "codellama" before "llama".
    family_keywords: list[tuple[str, str]] = [
        ("tinyllama", "tinyllama"),
        ("codellama", "codellama"),
        ("codestral", "codestral"),
        ("starcoder", "starcoder"),
        ("deepseek", "deepseek"),
        ("mixtral", "mixtral"),
        ("mistral", "mistral"),
        ("llama", "llama"),
        ("phi", "phi"),
        ("qwen", "qwen"),
        ("gemma", "gemma"),
        ("falcon", "falcon"),
        ("mpt", "mpt"),
    ]
    for keyword, label in family_keywords:
        if keyword in name:
            return label
    return "unknown"


# ── models list ───────────────────────────────────────────────────────────────


def _models_list(
    models_dir: Path,
    *,
    objective: str | None = None,
    family: str | None = None,
    quantization: str | None = None,
    file_type: str | None = None,
    min_size_gb: float | None = None,
    max_size_gb: float | None = None,
) -> int:
    """Print a table of model files in models_dir.

    Args:
        models_dir: Directory to scan.
        objective: Optional objective/category filename filter.
        family: Optional model family filename filter.
        quantization: Optional quantization filename filter.
        file_type: Optional suffix filter such as ``gguf`` or ``safetensors``.
        min_size_gb: Optional minimum local file size in GB.
        max_size_gb: Optional maximum local file size in GB.

    Returns:
        0 always (no failure mode — empty directory is informational).
    """
    model_files = _iter_model_files(
        models_dir,
        objective=objective,
        family=family,
        quantization=quantization,
        file_type=file_type,
        min_size_gb=min_size_gb,
        max_size_gb=max_size_gb,
    )
    if not model_files:
        _write_line(f"No matching model files found in {models_dir}")
        return 0

    if _RICH_AVAILABLE and _console is not None:
        table = _Table(title=f"Models in {models_dir}", show_lines=True)
        table.add_column("Name", style="cyan", no_wrap=False, overflow="fold")
        table.add_column("Type", style="blue")
        table.add_column("Size", justify="right", style="green")
        table.add_column("Quantization", style="yellow")
        table.add_column("Family", style="magenta")
        table.add_column("Last Modified")
        for fp in model_files:
            stat = fp.stat()
            size_gb = stat.st_size / (1024**3)
            mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            table.add_row(
                fp.name,
                _guess_model_file_type(fp),
                f"{size_gb:.2f} GB",
                _guess_quantization(fp.name),
                _guess_family(fp.name),
                mtime,
            )
        _console.print(table)
    else:
        header = f"{'Name':<50} {'Type':<10} {'Size':>8}  {'Quant':<10}  {'Family':<12}  {'Modified'}"
        _write_line(header)
        _write_line("-" * len(header))
        for fp in model_files:
            stat = fp.stat()
            size_gb = stat.st_size / (1024**3)
            mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).strftime("%Y-%m-%d")
            quant = _guess_quantization(fp.name)
            family = _guess_family(fp.name)
            file_kind = _guess_model_file_type(fp)
            _write_line(f"{fp.name:<50} {file_kind:<10} {size_gb:>7.2f}G  {quant:<10}  {family:<12}  {mtime}")
    return 0


# ── models download ────────────────────────────────────────────────────────────


def _models_remove(name: str | None, models_dir: Path) -> int:
    """Delete a model file after prompting for confirmation.

    Args:
        name: Filename (or partial name) of the model to remove.
        models_dir: Directory to search for the model.

    Returns:
        0 on success, 1 if no matching model found or removal declined.
    """
    if not name:
        _write_line("Error: --name is required for remove.")
        return 1

    candidates = _find_model_candidates(name, models_dir)
    if candidates is None:
        _write_line("Error: --name must be a safe model filename fragment.")
        return 1
    if not candidates:
        _write_line(f"No model matching '{name}' found in {models_dir}")
        return 1

    if len(candidates) > 1:
        _write_line(f"Multiple models match '{name}':")
        for fp in candidates:
            _write_line(f"  {fp.name}")
        _write_line("Please provide a more specific --name.")
        return 1

    target = candidates[0]
    size_gb = target.stat().st_size / (1024**3)
    _write_line(f"About to delete: {target} ({size_gb:.2f} GB)")
    try:
        confirm = input("Are you sure? [y/N]: ").strip().lower()
    except EOFError:
        confirm = "n"
    if confirm != "y":
        _write_line("Aborted.")
        return 1

    try:
        target.unlink()
        _write_line(f"Deleted: {target.name}")
        return 0
    except OSError as exc:
        _write_line(f"Could not delete {target.name}: {exc}")
        logger.warning("Model file deletion failed for %s: %s", target.name, exc)
        return 1


def _models_info(name: str | None, models_dir: Path) -> int:
    """Print detailed metadata for a specific model file.

    Args:
        name: Filename (or partial name) of the model.
        models_dir: Directory to search.

    Returns:
        0 on success, 1 if no matching model found.
    """
    if not name:
        _write_line("Error: --name is required for info.")
        return 1

    candidates = _find_model_candidates(name, models_dir)
    if candidates is None:
        _write_line("Error: --name must be a safe model filename fragment.")
        return 1
    if not candidates:
        _write_line(f"No model matching '{name}' found in {models_dir}")
        return 1

    target = candidates[0]
    stat = target.stat()
    size_bytes = stat.st_size
    size_gb = size_bytes / (1024**3)
    mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
    quant = _guess_quantization(target.name)
    family = _guess_family(target.name)

    # Peek at GGUF header for magic bytes validation
    gguf_valid = False
    try:
        with target.open("rb") as fh:
            gguf_valid = fh.read(4) == b"GGUF"
    except OSError:
        gguf_valid = False

    _write_line(f"Model: {target.name}")
    _write_line(f"  Path         : {target}")
    _write_line(f"  Size         : {size_bytes:,} bytes ({size_gb:.2f} GB)")
    _write_line(f"  Modified     : {mtime}")
    _write_line(f"  Quantization : {quant}")
    _write_line(f"  Family       : {family}")
    _write_line(f"  GGUF header  : {'valid' if gguf_valid else 'INVALID or unreadable'}")
    return 0


def _models_recommend(vram_gb: float) -> int:
    """Print model recommendations based on available VRAM.

    Args:
        vram_gb: Available VRAM in GB (0.0 for CPU-only).

    Returns:
        0 always.
    """
    candidates = _get_recommended_models(vram_gb)
    _write_line(f"Recommended models for {vram_gb:.1f} GB VRAM:")
    for model in candidates:
        _write_line(f"  {model['name']}")
        _write_line(f"    Repo     : {model['repo']}")
        _write_line(f"    Backend  : {model.get('backend', 'llama_cpp')}")
        _write_line(f"    Format   : {model.get('format', 'gguf')}")
        if model.get("filename"):
            _write_line(f"    Filename : {model['filename']}")
        _write_line(f"    URL      : {model['url']}")
        if model.get("backend") in _NATIVE_BACKENDS:
            _write_line(
                "    Download : "
                f"vetinari models download --repo {model['repo']} "
                f"--backend {model['backend']} --format {model.get('format', 'safetensors')}"
            )
        else:
            _write_line(
                f"    Download : vetinari models download --repo {model['repo']} --filename {model['filename']}"
            )
        _write_line()
    return 0


def _models_scan() -> int:
    """Scan common directories for .gguf and .awq model files.

    Uses the setup module's scan function to discover models across
    VETINARI_MODELS_DIR, ~/.vetinari/models, ~/.cache/huggingface, etc.

    Returns:
        0 always.
    """
    from vetinari.setup.init_wizard import _scan_for_models

    _write_line("[AM Workbench] Scanning for model files...")
    found = _scan_for_models()
    if not found:
        _write_line("  No .gguf or .awq files found.")
        _write_line("  Run 'vetinari init' to download a recommended model.")
        return 0
    _write_line(f"  Found {len(found)} model file(s):")
    for model_path in found:
        size_mb = model_path.stat().st_size / (1024 * 1024) if model_path.exists() else 0
        _write_line(f"    {model_path.name:40s} {size_mb:>8.1f} MB  {model_path.parent}")
    return 0


def _models_check() -> int:
    """Check for newer, better models using benchmarks and community sentiment.

    Reads cached upgrade candidates from the last weekly freshness check,
    or runs a fresh check if none have been performed yet.

    Returns:
        0 on success, 1 on failure.
    """
    from vetinari.models.model_scout import ModelFreshnessChecker

    checker = ModelFreshnessChecker()

    # Try cached results first
    upgrades = checker.get_cached_upgrades()

    if not upgrades:
        _write_line("[AM Workbench] No cached model check results found. Running fresh check...")
        _write_line("  This may take a moment (searching HuggingFace, benchmarks, sentiment)...")
        upgrades = checker.check_for_upgrades()

    if not upgrades:
        _write_line("[AM Workbench] Your models are up to date — no better alternatives found.")
        return 0

    _write_line(f"[AM Workbench] Found {len(upgrades)} potential upgrade(s):\n")

    for i, u in enumerate(upgrades, 1):
        _write_line(f"  {i}. {u.candidate_name}")
        _write_line(f"     Replaces:  {u.current_model_id}")
        _write_line(
            f"     Score:     {u.overall_score:.2f} (benchmark={u.benchmark_score:.2f}, sentiment={u.sentiment_score:.2f})"
        )
        if u.available_formats:
            _write_line(f"     Formats:   {', '.join(u.available_formats)}")
        if u.vram_estimate_gb > 0:
            _write_line(f"     VRAM:      ~{u.vram_estimate_gb:.1f} GB")
        _write_line(f"     Reason:    {u.reason}")
        if u.candidate_repo_id:
            native_format = next((fmt for fmt in ("awq", "gptq", "safetensors") if fmt in u.available_formats), "")
            if native_format:
                _write_line(
                    "     Download:  "
                    f"vetinari models download --repo {u.candidate_repo_id} --backend vllm --format {native_format}"
                )
            else:
                _write_line("     Download:  choose a GGUF filename from the repo, then run:")
                _write_line(
                    f"                vetinari models download --repo {u.candidate_repo_id} --filename <file.gguf>"
                )
        _write_line()

    return 0


# ── cmd_models ────────────────────────────────────────────────────────────────
