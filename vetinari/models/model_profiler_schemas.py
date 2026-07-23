"""Data structures and constants for ModelProfiler.

Contains all dataclasses, lookup tables, and configuration constants used by
``ModelProfiler`` and related modules. This separation keeps computational logic
in ``model_profiler_data.py`` while allowing the data definitions to grow
independently.

Tables included:
- KV cache size by quantization
- Architecture family detection patterns
- Temperature matrices for task/model combinations
- Chat format detection
- RoPE frequency overrides
"""

from __future__ import annotations

from dataclasses import dataclass

# ── KV cache bytes per token by quantization type (approximate) ───────────────
# Source: llama.cpp documentation and empirical measurement
KV_BYTES_PER_TOKEN: dict[str, float] = {
    "f16": 2.0,
    "q8_0": 1.0,
    "q4_0": 0.5,
    "q4_1": 0.5625,
    "q5_0": 0.625,
    "q5_1": 0.6875,
}

# Default overhead reserved for llama.cpp runtime (GB)
RUNTIME_OVERHEAD_GB = 1.5

# GPU safety margin (fraction withheld from free VRAM to avoid OOM)
GPU_SAFETY_MARGIN = 0.05  # 5%

# ── Architecture family detection patterns ────────────────────────────────────
FAMILY_PATTERNS: list[tuple[str, str]] = [
    (r"llama", "llama"),
    (r"qwen2", "qwen2"),
    (r"qwen3", "qwen3"),
    (r"mistral", "mistral"),
    (r"mixtral", "mixtral"),
    (r"gemma", "gemma"),
    (r"phi3", "phi3"),
    (r"phi", "phi"),
    (r"starcoder", "starcoder"),
    (r"deepseek", "deepseek"),
    (r"yi", "yi"),
    (r"command-r", "command-r"),
    (r"internlm", "internlm"),
    (r"baichuan", "baichuan"),
    (r"falcon", "falcon"),
    (r"mpt", "mpt"),
    (r"gpt2", "gpt2"),
    (r"codellama", "codellama"),
    (r"stablelm", "stablelm"),
    (r"glm", "glm"),
]

# ── Temperature matrix: family x task_type ────────────────────────────────────
# Base temperatures before quant offsets.  Lower = more deterministic.
TEMPERATURE_MATRIX: dict[str, dict[str, float]] = {
    "llama": {
        "coding": 0.05,
        "reasoning": 0.3,
        "general": 0.5,
        "creative": 0.8,
        "instruction": 0.3,
    },
    "qwen2": {
        "coding": 0.02,
        "reasoning": 0.25,
        "general": 0.4,
        "creative": 0.75,
        "instruction": 0.25,
    },
    "qwen3": {
        "coding": 0.02,
        "reasoning": 0.25,
        "general": 0.4,
        "creative": 0.75,
        "instruction": 0.25,
    },
    "mistral": {
        "coding": 0.05,
        "reasoning": 0.3,
        "general": 0.5,
        "creative": 0.8,
        "instruction": 0.3,
    },
    "mixtral": {
        "coding": 0.05,
        "reasoning": 0.3,
        "general": 0.5,
        "creative": 0.8,
        "instruction": 0.3,
    },
    "gemma": {
        "coding": 0.05,
        "reasoning": 0.3,
        "general": 0.5,
        "creative": 0.7,
        "instruction": 0.3,
    },
    "phi3": {
        "coding": 0.05,
        "reasoning": 0.2,
        "general": 0.4,
        "creative": 0.7,
        "instruction": 0.2,
    },
    "phi": {
        "coding": 0.05,
        "reasoning": 0.2,
        "general": 0.4,
        "creative": 0.7,
        "instruction": 0.2,
    },
    "deepseek": {
        "coding": 0.0,
        "reasoning": 0.2,
        "general": 0.4,
        "creative": 0.7,
        "instruction": 0.2,
    },
    "starcoder": {
        "coding": 0.02,
        "reasoning": 0.3,
        "general": 0.5,
        "creative": 0.7,
        "instruction": 0.3,
    },
    "codellama": {
        "coding": 0.02,
        "reasoning": 0.3,
        "general": 0.5,
        "creative": 0.7,
        "instruction": 0.3,
    },
    "glm": {
        "coding": 0.05,
        "reasoning": 0.3,
        "general": 0.5,
        "creative": 0.8,
        "instruction": 0.3,
    },
}

# Quantization offsets: lower-precision quants get slightly higher temperature
# to compensate for reduced output diversity.
QUANT_TEMP_OFFSETS: dict[str, float] = {
    "q2_k": 0.08,
    "q2_k_s": 0.08,
    "q2_k_m": 0.07,
    "q3_k_s": 0.07,
    "q3_k_m": 0.06,
    "q3_k_l": 0.05,
    "q4_0": 0.05,
    "q4_1": 0.04,
    "q4_k_s": 0.04,
    "q4_k_m": 0.03,
    "q5_0": 0.02,
    "q5_1": 0.02,
    "q5_k_s": 0.02,
    "q5_k_m": 0.01,
    "q6_k": 0.01,
    "q8_0": 0.0,
    "f16": 0.0,
    "bf16": 0.0,
}

# Default temperature for unknown families
DEFAULT_TEMPERATURES: dict[str, float] = {
    "coding": 0.05,
    "reasoning": 0.3,
    "general": 0.5,
    "creative": 0.8,
    "instruction": 0.3,
}

# Rope frequency overrides for models that support extended context.
# Models with NTK-aware rope scaling need a higher freq base for long contexts.
ROPE_FREQ_BASE_OVERRIDES: dict[str, float] = {
    "llama": 500000.0,  # Llama 3 uses 500k rope base
    "qwen2": 1000000.0,  # Qwen2.5 uses 1M rope base
    "qwen3": 1000000.0,
    "mistral": 1000000.0,
    "deepseek": 100000.0,
}

# Chat format detection from architecture family
CHAT_FORMAT_MAP: dict[str, str] = {
    "llama": "llama-3",
    "qwen2": "chatml",
    "qwen3": "chatml",
    "mistral": "mistral-instruct",
    "mixtral": "mistral-instruct",
    "gemma": "gemma",
    "phi3": "chatml",
    "phi": "chatml",
    "deepseek": "chatml",
    "starcoder": "chatml",
    "codellama": "llama-2",
    "glm": "chatglm4",
    "yi": "chatml",
    "command-r": "command-r",
}


# ── Data classes ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class GGUFMetadata:
    """GGUF file header metadata extracted from a model file.

    All fields are read from the GGUF key-value pairs; fields not present
    in the file default to ``""`` or ``0``.
    """

    architecture: str = ""
    block_count: int = 0
    head_count: int = 0
    head_count_kv: int = 0
    context_length: int = 0
    expert_count: int = 0
    expert_used_count: int = 0
    file_type: int = 0
    embedding_length: int = 0
    vocab_size: int = 0
    quantization: str = ""
    file_size_gb: float = 0.0

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return (
            f"GGUFMetadata(arch={self.architecture!r}, layers={self.block_count}, "
            f"ctx={self.context_length}, experts={self.expert_count}, "
            f"quant={self.quantization!r}, size={self.file_size_gb:.1f}GB)"
        )


@dataclass(slots=True)
class ModelProfile:
    """Complete computed profile for a local GGUF model.

    Combines metadata from the GGUF header with computed optimal
    parameters for the Llama() constructor and sampling.
    """

    model_id: str
    family: str
    metadata: GGUFMetadata
    n_ctx: int
    n_gpu_layers: int
    artifact_path: str = ""
    artifact_sha256: str = ""
    artifact_size_bytes: int = 0
    artifact_mtime_ns: int = 0
    n_batch: int = 512
    use_mlock: bool = True
    chat_format: str | None = None
    seed: int = -1  # -1 = random seed
    rope_freq_base: float | None = None
    flash_attn: bool = True
    kv_type: str = "q8_0"

    # Sampling defaults (per-family, pre-quant-offset)
    base_temperatures: dict[str, float] = None

    def __post_init__(self) -> None:
        """Initialize default base_temperatures if not provided."""
        if self.base_temperatures is None:
            object.__setattr__(self, "base_temperatures", DEFAULT_TEMPERATURES.copy())

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return (
            f"ModelProfile(id={self.model_id!r}, family={self.family!r}, "
            f"ctx={self.n_ctx}, gpu_layers={self.n_gpu_layers}, "
            f"batch={self.n_batch}, chat={self.chat_format!r})"
        )

    def get_llama_kwargs(self) -> dict[str, any]:
        """Build kwargs dict for the Llama() constructor.

        Returns:
            Dictionary of keyword arguments suitable for passing to
            ``llama_cpp.Llama()``.
        """
        kwargs: dict[str, any] = {
            "n_gpu_layers": self.n_gpu_layers,
            "n_ctx": self.n_ctx,
            "n_batch": self.n_batch,
            "use_mlock": self.use_mlock,
            "flash_attn": self.flash_attn,
            "verbose": False,
        }

        if self.chat_format:
            kwargs["chat_format"] = self.chat_format
        if self.seed >= 0:
            kwargs["seed"] = self.seed
        if self.rope_freq_base is not None:
            kwargs["rope_freq_base"] = self.rope_freq_base

        return kwargs

    def to_dict(self) -> dict[str, any]:
        """Serialize to a JSON-compatible dictionary.

        Returns:
            Plain dict with all profile fields.
        """
        from dataclasses import asdict

        return asdict(self)
