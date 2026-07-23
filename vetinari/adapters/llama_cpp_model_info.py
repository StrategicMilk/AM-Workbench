"""llama.cpp model info helpers — constants, capability inference, and loaded-model tracking.

Contains the static configuration and pure helper functions for the llama.cpp
local inference backend:
- ``DEFAULT_MEMORY_BUDGET_GB``, ``DEFAULT_RAM_BUDGET_GB`` — resource budget defaults
- ``_CAPABILITY_PATTERNS``, ``_CONTEXT_PATTERNS`` — regex tables for model ID parsing
- ``_VRAM_OVERHEAD_FACTOR``, ``_JSON_RESPONSE_FORMAT``, ``_EMPTY_CHOICES_FALLBACK``,
  ``_EMPTY_DELTA_FALLBACK`` — per-request constants allocated once at module load
- ``_infer_capabilities(model_id)`` — tag list from filename patterns
- ``_infer_context_window(model_id)`` — context size from model family
- ``_estimate_memory_gb(file_path)`` — VRAM estimate from GGUF file size
- ``verify_gguf_checksum(file_path, expected_sha256)`` — SHA-256 integrity check
- ``_model_id_from_path(file_path)`` — derive model ID from GGUF path
- ``_friendly_model_name(model_id)`` — clean display name from raw ID
- ``_LoadedModel`` — dataclass tracking a live llama_cpp.Llama instance

Separated from ``llama_cpp_adapter.py`` so these pure utilities can be unit-tested
without instantiating a full inference adapter or importing llama_cpp.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vetinari.constants import DEFAULT_CONTEXT_LENGTH
from vetinari.models.vram_capacity import KV_BYTES_PER_TOKEN

logger = logging.getLogger(__name__)


# ── Default resource budgets ──────────────────────────────────────────────────

DEFAULT_MEMORY_BUDGET_GB = 32  # RTX 5090 VRAM budget in GB
DEFAULT_RAM_BUDGET_GB = 30  # CPU RAM available for offloaded layers in GB

# ── Capability and context-window inference tables ────────────────────────────

_CAPABILITY_PATTERNS: list[tuple[str, list[str]]] = [
    (r"\bvl\b|vision.?language|multimodal|visual", ["vision", "coding", "reasoning"]),
    (r"uncensored|heretic|unfiltered|abliterated", ["uncensored", "reasoning"]),
    (r"thinking|cot|chain.?of.?thought", ["reasoning", "analysis"]),
    (r"coder|codestral|deepseek.?coder|starcoder|code.?llama|codegen", ["coding", "fast"]),
    (r"math|science|stem|minerva", ["reasoning", "analysis"]),
    (r"instruct|chat|assistant", ["coding", "reasoning"]),
    (r"qwen\d|qwen3", ["reasoning", "coding"]),
    (r"llama.?3", ["reasoning", "coding"]),
    (r"mistral|mixtral", ["reasoning", "coding"]),
    (r"phi.?\d", ["reasoning", "fast"]),
    (r"gemma", ["reasoning"]),
    (r"yi.?\d", ["reasoning"]),
]

_CONTEXT_PATTERNS: list[tuple[str, int]] = [
    # Llama 3.1 / 3.2 / 3.3 and explicit "-128k" variants: 128k context window.
    # Plain "llama-3" / "llama3" without a subversion digit is the 8k base family.
    # Pattern order matters: the specific subversion patterns must appear before the
    # generic llama-3 catch-all so that more-specific matches take priority.
    (r"llama.?3\.[1-9]", 131072),
    (r"llama.?3.*128k", 131072),
    (r"llama.?3", 8192),  # Llama 3 base / instruct (8B, 70B) — 8k context
    (r"qwen3", 32768),
    (r"qwen2\.5", 32768),
    (r"gemma.?2", 8192),
    (r"phi.?4", 16384),
    (r"phi.?3", 8192),  # Phi-3 supports 4k-128k; 8k is a safe default
    (r"mistral", 32768),
    (r"mixtral", 32768),
    (r"yi", 8192),  # Yi models support 4k-200k; 8k is a safe default
]

# GGUF file size to approximate VRAM usage (rough: file_size * 1.1 for KV cache overhead)
_WEIGHTS_OVERHEAD_FACTOR = 1.05

# Per-request constant — allocated once, reused on every JSON-mode call
_JSON_RESPONSE_FORMAT: dict[str, str] = {"type": "json_object"}

# Streaming fallback constants — allocated once, reused per-token
_EMPTY_CHOICES_FALLBACK: list[dict] = [{}]
_EMPTY_DELTA_FALLBACK: dict = {}
_GGUF_MAGIC = b"GGUF"
_GGUF_MIN_HEADER_BYTES = 24


class GGUFIntegrityError(ValueError):
    """Raised when a GGUF file fails deterministic pre-load validation."""


# ── Pure helper functions ─────────────────────────────────────────────────────


def _infer_capabilities(model_id: str) -> list[str]:
    """Infer capability tags from a model ID or filename string.

    Scans the lowercase model ID against ``_CAPABILITY_PATTERNS`` using regex.
    Always includes ``"general"`` so every model has at least one tag.

    Args:
        model_id: Model identifier or filename to analyze.

    Returns:
        Sorted list of inferred capability tag strings.
    """
    lower = model_id.lower()
    caps: set[str] = set()
    for pattern, tags in _CAPABILITY_PATTERNS:
        if re.search(pattern, lower):
            caps.update(tags)
    caps.add("general")
    return sorted(caps)


def _infer_context_window(model_id: str) -> int:
    """Infer context window size from model family patterns.

    Scans the lowercase model ID against ``_CONTEXT_PATTERNS``. Returns
    ``DEFAULT_CONTEXT_LENGTH`` when no pattern matches.

    Args:
        model_id: Model identifier or filename to analyze.

    Returns:
        Estimated context window size in tokens.
    """
    lower = model_id.lower()
    for pattern, ctx in _CONTEXT_PATTERNS:
        if re.search(pattern, lower):
            return ctx
    return DEFAULT_CONTEXT_LENGTH


def _estimate_memory_gb(file_path: Path) -> float:
    """Estimate VRAM usage from GGUF weights plus context KV-cache reserve.

    Uses measured GGUF file size as the weight basis, adds a small allocator
    reserve, and includes an explicit KV-cache reserve from the inferred
    context window instead of a fixed file-size multiplier.

    Args:
        file_path: Path to the .gguf file.

    Returns:
        Estimated memory usage in GB, rounded to one decimal place.
    """
    size_bytes = file_path.stat().st_size
    weights_gb = (size_bytes / (1024**3)) * _WEIGHTS_OVERHEAD_FACTOR
    context_len = _infer_context_window(_model_id_from_path(file_path))
    kv_gb = (context_len * KV_BYTES_PER_TOKEN["f16"]) / (1024**3)
    return round(weights_gb + kv_gb, 1)


def verify_gguf_checksum(file_path: Path, expected_sha256: str | None = None) -> bool:
    """Verify SHA256 checksum of a downloaded GGUF file.

    Computes the SHA256 hash of the file and compares against the expected
    value. Missing or malformed expected hashes fail closed because a local
    model artifact without an external digest cannot be authenticated.

    Args:
        file_path: Path to the downloaded GGUF file.
        expected_sha256: Expected SHA256 hex digest.

    Returns:
        True if checksum matches. False if the expected hash is missing,
        malformed, or mismatched.
    """
    if not expected_sha256 or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256.strip()):
        logger.warning("GGUF checksum verification failed for %s: expected sha256 is required", file_path.name)
        return False

    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    computed = sha256.hexdigest()

    if computed != expected_sha256.lower():
        logger.warning(
            "GGUF checksum mismatch for %s: expected=%s computed=%s",
            file_path.name,
            expected_sha256,
            computed,
        )
        return False

    logger.info("GGUF checksum verified for %s", file_path.name)
    return True


def validate_gguf_file(file_path: Path) -> None:
    """Validate a GGUF file before handing it to llama.cpp.

    Args:
        file_path: Path to the GGUF artifact.

    Raises:
        GGUFIntegrityError: If the file is missing, truncated, or does not
            start with the GGUF magic header.
    """
    if not file_path.is_file():
        raise GGUFIntegrityError(f"GGUF file missing: {file_path}")
    size = file_path.stat().st_size
    if size < _GGUF_MIN_HEADER_BYTES:
        raise GGUFIntegrityError(f"GGUF file too small: {file_path} ({size} bytes)")
    with file_path.open("rb") as handle:
        header = handle.read(_GGUF_MIN_HEADER_BYTES)
    if header[:4] != _GGUF_MAGIC:
        raise GGUFIntegrityError(f"GGUF magic header missing: {file_path}")
    version = int.from_bytes(header[4:8], byteorder="little", signed=False)
    if version < 1:
        raise GGUFIntegrityError(f"GGUF version is invalid: {version}")


def _model_id_from_path(file_path: Path) -> str:
    """Derive a model ID from a GGUF file path by stripping the extension.

    Args:
        file_path: Path to the .gguf file.

    Returns:
        Model identifier string (the filename stem).
    """
    return str(file_path.stem)


def _friendly_model_name(model_id: str) -> str:
    """Generate a human-friendly display name from a raw model ID.

    Strips common HuggingFace organisation prefixes (e.g. ``bartowski_``),
    extracts quantization suffixes into a parenthetical, and replaces
    hyphens and underscores with spaces.

    Args:
        model_id: Raw model identifier (typically the GGUF filename stem).

    Returns:
        Cleaned display name (e.g. ``"GLM 4.7 Flash (Q4_K_S)"``).
    """
    name = model_id

    # Strip common org prefixes (HuggingFace style: "org_ModelName")
    for prefix in (
        "zai-org_",
        "bartowski_",
        "mradermacher_",
        "TeichAI_",
        "unsloth_",
        "DavidAU_",
        "Merlinoz11_",
        "hugging-quants_",
    ):
        if name.startswith(prefix):
            name = name[len(prefix) :]
            break

    # Extract quantization suffix into a parenthetical tag
    quant = ""
    for q in (
        ".q4_k_m",
        ".q8_0",
        ".bf16",
        "-Q4_K_S",
        "-Q8_0",
        "-Q6_K",
        "_Q4_K_S",
        "_Q8_0",
        "_Q6_K",
        "-q8_0",
        "-q4_k_m",
    ):
        if name.lower().endswith(q.lower()):
            quant = name[len(name) - len(q) :]
            name = name[: len(name) - len(q)]
            break

    name = name.replace("-", " ").replace("_", " ").strip()

    if quant:
        name = f"{name} ({quant.strip('-_. ')})"

    return name


# ── Loaded model tracking ─────────────────────────────────────────────────────


@dataclass
class _LoadedModel:
    """Tracks a loaded llama.cpp model instance and its metadata.

    Attributes:
        model: The llama_cpp.Llama instance (any — avoids hard import).
        model_id: Unique string identifier for this model.
        file_path: Absolute path to the source .gguf file.
        memory_gb: Estimated VRAM usage in GB.
        last_used: Epoch timestamp of the most recent inference call (LRU eviction key).
        context_length: Context window size the model was loaded with.
    """

    model: Any  # llama_cpp.Llama instance
    model_id: str
    file_path: Path
    memory_gb: float
    last_used: float = field(default_factory=time.time)
    context_length: int = DEFAULT_CONTEXT_LENGTH

    def __repr__(self) -> str:
        return f"_LoadedModel(model_id={self.model_id!r}, context_length={self.context_length!r})"
