"""GGUF metadata reader helpers for model profiling."""

from __future__ import annotations

import logging
import re
from importlib import import_module
from pathlib import Path
from typing import Any, TypeAlias, cast

from vetinari.models.model_profiler_schemas import FAMILY_PATTERNS as _FAMILY_PATTERNS
from vetinari.models.model_profiler_schemas import GGUFMetadata as _SchemaGGUFMetadata

logger = logging.getLogger(__name__)

GGUFMetadata: TypeAlias = _SchemaGGUFMetadata


def read_metadata(model_path: Path) -> GGUFMetadata:
    """Read GGUF header metadata from a model file.

    Uses the ``gguf`` package for memory-mapped header-only reads (instant,
    no full-file load). Falls back to filename heuristics if the ``gguf``
    package is not installed.

    Args:
        model_path: Path to a ``.gguf`` file.

    Returns:
        Populated GGUFMetadata dataclass.
    """
    file_size_gb = model_path.stat().st_size / (1024**3) if model_path.exists() else 0.0

    try:
        gguf: Any = import_module("gguf")
        gguf_reader = gguf.GGUFReader

        reader = gguf_reader(str(model_path))
        kv = {f.name: f.data for f in reader.fields.values() if hasattr(f, "data")}

        arch = _extract_kv_string(kv, "general.architecture")

        # Architecture-prefixed keys (e.g., "llama.block_count")
        block_count = _extract_kv_int(kv, f"{arch}.block_count")
        head_count = _extract_kv_int(kv, f"{arch}.attention.head_count")
        head_count_kv = _extract_kv_int(kv, f"{arch}.attention.head_count_kv")
        context_length = _extract_kv_int(kv, f"{arch}.context_length")
        expert_count = _extract_kv_int(kv, "general.expert_count") or _extract_kv_int(kv, f"{arch}.expert_count")
        expert_used = _extract_kv_int(kv, "general.expert_used_count") or _extract_kv_int(
            kv, f"{arch}.expert_used_count"
        )
        file_type = _extract_kv_int(kv, "general.file_type")
        embedding_length = _extract_kv_int(kv, f"{arch}.embedding_length")
        vocab_size = _extract_kv_int(kv, f"{arch}.vocab_size") or _extract_kv_int(kv, "tokenizer.ggml.vocab_size")

        quantization = _detect_quantization(file_type, model_path.name)

        return GGUFMetadata(
            architecture=arch,
            block_count=block_count,
            head_count=head_count,
            head_count_kv=head_count_kv,
            context_length=context_length,
            expert_count=expert_count,
            expert_used_count=expert_used,
            file_type=file_type,
            embedding_length=embedding_length,
            vocab_size=vocab_size,
            quantization=quantization,
            file_size_gb=round(file_size_gb, 2),
        )

    except ImportError:
        logger.info("gguf package not installed; falling back to filename heuristics for %s", model_path.name)
        return _metadata_from_filename(model_path)
    except Exception as exc:
        logger.warning("Failed to read GGUF metadata from %s — falling back to heuristics: %s", model_path.name, exc)
        return _metadata_from_filename(model_path)


def _extract_kv_string(kv: dict, key: str) -> str:
    """Extract a string value from GGUF key-value data.

    Args:
        kv: Key-value mapping from GGUF reader.
        key: The GGUF metadata key to extract.

    Returns:
        String value, or empty string if not found.
    """
    val = kv.get(key)
    if val is None:
        return ""
    # gguf library may return bytes, numpy arrays, or lists
    if hasattr(val, "tobytes"):
        return cast(str, val.tobytes().decode("utf-8", errors="replace").rstrip("\x00"))
    if isinstance(val, bytes):
        return val.decode("utf-8", errors="replace").rstrip("\x00")
    if isinstance(val, (list, tuple)) and val:
        first = val[0] if len(val) == 1 else val
        if isinstance(first, (bytes, bytearray)):
            return bytes(first).decode("utf-8", errors="replace").rstrip("\x00")
        return str(first)
    return str(val)


def _extract_kv_int(kv: dict, key: str) -> int:
    """Extract an integer value from GGUF key-value data.

    Args:
        kv: Key-value mapping from GGUF reader.
        key: The GGUF metadata key to extract.

    Returns:
        Integer value, or 0 if not found or not convertible.
    """
    val = kv.get(key)
    if val is None:
        return 0
    # Handle numpy scalars and arrays
    if hasattr(val, "item"):
        return int(val.item())
    if isinstance(val, (list, tuple)) and val:
        first = val[0]
        if hasattr(first, "item"):
            return int(first.item())
        return int(first)
    try:
        return int(val)
    except (TypeError, ValueError):
        logger.warning("Could not parse GGUF value as integer for key %r — using 0 as fallback", key)
        return 0


def _detect_quantization(file_type: int, filename: str) -> str:
    """Detect the quantization type from GGUF file_type code or filename.

    Args:
        file_type: GGUF general.file_type integer code.
        filename: Model filename for pattern matching fallback.

    Returns:
        Quantization string like ``"q4_k_m"``, ``"q8_0"``, ``"f16"``.
    """
    # GGUF file_type codes (from llama.cpp ggml-common.h)
    _file_type_map: dict[int, str] = {
        0: "f32",
        1: "f16",
        2: "q4_0",
        3: "q4_1",
        7: "q8_0",
        8: "q5_0",
        9: "q5_1",
        10: "q2_k",
        11: "q3_k_s",
        12: "q3_k_m",
        13: "q3_k_l",
        14: "q4_k_s",
        15: "q4_k_m",
        16: "q5_k_s",
        17: "q5_k_m",
        18: "q6_k",
    }
    if file_type in _file_type_map:
        return _file_type_map[file_type]

    # Fallback: extract from filename
    lower = filename.lower()
    for quant in ("q4_k_m", "q4_k_s", "q5_k_m", "q5_k_s", "q6_k", "q8_0", "q4_0", "q5_0", "q5_1", "f16", "bf16"):
        if quant in lower:
            return quant
    return "unknown"


def _metadata_from_filename(model_path: Path) -> GGUFMetadata:
    """Infer approximate metadata from filename when GGUF reader is unavailable.

    Args:
        model_path: Path to the .gguf file.

    Returns:
        GGUFMetadata with best-effort values from filename patterns.
    """
    name = model_path.stem.lower()
    file_size_gb = model_path.stat().st_size / (1024**3) if model_path.exists() else 0.0

    arch = ""
    for pattern, family in _FAMILY_PATTERNS:
        if re.search(pattern, name):
            arch = family
            break

    # Estimate block count from parameter size (rough heuristic: layers ≈ params_b * 4)
    param_match = re.search(r"(\d+)[bB]", name)
    params_b = int(param_match.group(1)) if param_match else 7
    block_count = max(16, params_b * 4)

    quantization = _detect_quantization(0, model_path.name)

    return GGUFMetadata(
        architecture=arch,
        block_count=block_count,
        context_length=0,  # Unknown from filename alone
        quantization=quantization,
        file_size_gb=round(file_size_gb, 2),
    )


# ── Core calculation functions ────────────────────────────────────────────────
