"""Static backend install catalog data."""

from __future__ import annotations

from vetinari.types import ModelProvider

PROVIDER_ALIASES = {
    "llama_cpp": ModelProvider.LOCAL,
    "llama_cpp_gguf": ModelProvider.LOCAL,
    "local": ModelProvider.LOCAL,
    "vllm": ModelProvider.VLLM,
    "nim": ModelProvider.NIM,
    "nvidia_nim": ModelProvider.NIM,
    "sglang": ModelProvider.SGLANG,
}

PROVIDER_EXTRAS: dict[ModelProvider, tuple[str, ...]] = {
    ModelProvider.LOCAL: ("local",),
    ModelProvider.VLLM: ("vllm",),
    ModelProvider.NIM: (),
    ModelProvider.SGLANG: ("sglang",),
    ModelProvider.COMFYUI: ("comfyui",),
    ModelProvider.FASTER_WHISPER: ("audio",),
    ModelProvider.OPENAI: ("cloud",),
    ModelProvider.ANTHROPIC: ("cloud",),
    ModelProvider.GEMINI: ("cloud",),
}

ALL_INSTALLABLE_PROVIDERS: tuple[ModelProvider, ...] = (
    ModelProvider.LOCAL,
    ModelProvider.VLLM,
    ModelProvider.NIM,
    ModelProvider.SGLANG,
    ModelProvider.COMFYUI,
    ModelProvider.FASTER_WHISPER,
    ModelProvider.OPENAI,
    ModelProvider.ANTHROPIC,
    ModelProvider.GEMINI,
)

PROVIDER_IMPORTS: dict[ModelProvider, tuple[str, ...]] = {
    ModelProvider.LOCAL: ("llama_cpp",),
    ModelProvider.COMFYUI: ("diffusers", "torch", "transformers"),
    ModelProvider.FASTER_WHISPER: ("faster_whisper",),
    ModelProvider.OPENAI: ("openai",),
    ModelProvider.ANTHROPIC: ("anthropic",),
    ModelProvider.GEMINI: ("google.genai",),
}

PROVIDER_ENV: dict[ModelProvider, tuple[str, ...]] = {
    ModelProvider.VLLM: ("VETINARI_VLLM_ENDPOINT",),
    ModelProvider.NIM: ("VETINARI_NIM_ENDPOINT", "NGC_API_KEY"),
    ModelProvider.SGLANG: ("VETINARI_SGLANG_ENDPOINT",),
    ModelProvider.COMFYUI: ("VETINARI_COMFYUI_ENDPOINT", "COMFYUI_ENDPOINT"),
    ModelProvider.OPENAI: ("OPENAI_API_KEY",),
    ModelProvider.ANTHROPIC: ("ANTHROPIC_API_KEY", "CLAUDE_API_KEY"),
    ModelProvider.GEMINI: ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
}

PROVIDER_ENVIRONMENT_GROUP: dict[ModelProvider, str] = {
    ModelProvider.LOCAL: "default",
    ModelProvider.VLLM: "vllm",
    ModelProvider.NIM: "nim-container",
    ModelProvider.SGLANG: "sglang",
    ModelProvider.COMFYUI: "vision",
    ModelProvider.FASTER_WHISPER: "audio",
    ModelProvider.OPENAI: "cloud",
    ModelProvider.ANTHROPIC: "cloud",
    ModelProvider.GEMINI: "cloud",
}

CURRENT_ENVIRONMENT_SAFE_GROUPS = frozenset({
    "default",
    "external-runtime",
    "audio",
    "cloud",
    "vision",
    "nim-container",
})

ISOLATED_ENVIRONMENT_GROUPS = frozenset({"vllm", "sglang"})
TRAINING_ISOLATED_PROVIDERS = frozenset({ModelProvider.VLLM, ModelProvider.SGLANG})
DEFAULT_VLLM_MODEL = "Qwen/Qwen2.5-3B-Instruct"

__all__ = [
    "ALL_INSTALLABLE_PROVIDERS",
    "CURRENT_ENVIRONMENT_SAFE_GROUPS",
    "DEFAULT_VLLM_MODEL",
    "ISOLATED_ENVIRONMENT_GROUPS",
    "PROVIDER_ALIASES",
    "PROVIDER_ENV",
    "PROVIDER_ENVIRONMENT_GROUP",
    "PROVIDER_EXTRAS",
    "PROVIDER_IMPORTS",
    "TRAINING_ISOLATED_PROVIDERS",
]
