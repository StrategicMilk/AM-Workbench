"""Install wizard state and static option catalogs."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

INSTALL_WIZARD_STEPS = 6
INSTALL_WIZARD_TOTAL_STEPS = 9

FALLBACK_BACKENDS = [
    "llama_cpp",
    "nim",
    "comfyui",
    "faster_whisper",
    "openai",
    "anthropic",
    "gemini",
]

BACKEND_DISPLAY = {
    "llama_cpp": "llama-cpp-python (local GGUF, CPU/GPU)",
    "nim": "NVIDIA NIM (CUDA, enterprise)",
    "openai": "OpenAI API (cloud)",
    "anthropic": "Anthropic API (cloud)",
    "gemini": "Gemini API (cloud)",
}

CLOUD_PROVIDERS = [
    ("openai", "OpenAI", "OPENAI_API_KEY"),
    ("anthropic", "Anthropic", "ANTHROPIC_API_KEY"),
]

# TheBloke uploader deprecated 2024; switched to bartowski mirror.
DEFAULT_GGUF_MODEL_REPO_ID = "bartowski/Mistral-7B-Instruct-v0.3-GGUF"
DEFAULT_GGUF_MODEL_FILENAME = "Mistral-7B-Instruct-v0.3-Q4_K_M.gguf"

# ADR-0065 rejected SGLang, and the 2026-06-09 AM Engine decision removed vLLM setup.
BACKEND_MODEL_PROVISION_HINTS: dict[str, str] = {
    "nim": "Pull a NIM container via `docker pull nvcr.io/nim/<org>/<model>:<tag>` (NVIDIA NGC catalog).",
    "faster_whisper": "Provide a CTranslate2 repo ID, e.g. `Systran/faster-whisper-large-v3`.",
    "openai": "No local model needed - set OPENAI_API_KEY and the API handles models remotely.",
    "anthropic": "No local model needed - set ANTHROPIC_API_KEY and the API handles models remotely.",
    "gemini": "No local model needed - set GEMINI_API_KEY and the API handles models remotely.",
}


@dataclass
class InstallWizardResult:
    """Outcome of a guided install wizard run."""

    success: bool = False
    install_root: Path | None = None
    models_dir: Path | None = None
    selected_backend: str = ""
    cloud_keys_set: dict[str, bool] = field(default_factory=dict)
    config_path: Path | None = None
    errors: list[str] = field(default_factory=list)

    def __repr__(self) -> str:
        return (
            f"InstallWizardResult(success={self.success!r}, "
            f"backend={self.selected_backend!r}, config_path={self.config_path!r})"
        )


__all__ = [
    "BACKEND_DISPLAY",
    "BACKEND_MODEL_PROVISION_HINTS",
    "CLOUD_PROVIDERS",
    "DEFAULT_GGUF_MODEL_FILENAME",
    "DEFAULT_GGUF_MODEL_REPO_ID",
    "FALLBACK_BACKENDS",
    "INSTALL_WIZARD_STEPS",
    "INSTALL_WIZARD_TOTAL_STEPS",
    "InstallWizardResult",
]
