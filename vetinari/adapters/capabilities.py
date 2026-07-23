"""Backend capability declarations for model catalog backends."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum

from vetinari.adapters.base import ProviderType

# AM Engine architecture decision 2026-06-09: AM_ENGINE is the supervised,
# durable first-party path; LOCAL remains available during the opt-in stage.


class CacheDurabilityLevel(str, Enum):
    """How much cache state Vetinari can rely on for a backend."""

    NONE = "none"
    SEMANTIC_CACHE_ONLY = "semantic_cache_only"
    PREFIX_CACHE_REUSE = "prefix_cache_reuse"
    VOLATILE_KV_STATE = "volatile_kv_state"
    DURABLE_KV_SNAPSHOT = "durable_kv_snapshot"


@dataclass(frozen=True, slots=True)
class BackendCapabilityProfile:
    """Serializable backend capability profile."""

    provider_type: ProviderType
    modalities: tuple[str, ...]
    substrates: tuple[str, ...] = ("windows_native",)
    lifecycle_control: str = "manual"
    load_unload_control: bool = False
    exclusive_gpu_lease: bool = False
    vram_accounting: str = "unknown"
    prefix_cache: bool = False
    cache_durability: CacheDurabilityLevel = CacheDurabilityLevel.NONE
    cache_scope: str = "none"
    cache_identity_requirements: tuple[str, ...] = ()
    supports_mid_generation_resume: bool = False
    requires_supervisor: bool = False
    requires_docker: bool = False
    requires_linux: bool = False
    notes: str = ""

    def __repr__(self) -> str:
        return (
            "BackendCapabilityProfile("
            f"provider_type={self.provider_type.value!r}, "
            f"modalities={self.modalities!r}, substrates={self.substrates!r}, "
            f"cache_durability={self.cache_durability.value!r})"
        )

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable dict.

        Returns:
            Dict representation suitable for API and config serialization.
        """
        data = asdict(self)
        data["provider_type"] = self.provider_type.value
        data["cache_durability"] = self.cache_durability.value
        return data


def _local_text_backend_capabilities() -> dict[ProviderType, BackendCapabilityProfile]:
    """Return local/server text backend capability profiles."""
    return {
        ProviderType.LOCAL: BackendCapabilityProfile(
            provider_type=ProviderType.LOCAL,
            modalities=("text",),
            load_unload_control=True,
            vram_accounting="llama_cpp",
            cache_durability=CacheDurabilityLevel.VOLATILE_KV_STATE,
            cache_scope="process",
            notes="llama.cpp state is volatile until LW-RT-CACHE-DURABILITY lands.",
        ),
        ProviderType.AM_ENGINE: BackendCapabilityProfile(
            provider_type=ProviderType.AM_ENGINE,
            modalities=("text", "vision"),
            substrates=("windows_native", "linux_native"),
            lifecycle_control="supervised",
            load_unload_control=True,
            exclusive_gpu_lease=True,
            vram_accounting="ledger",
            prefix_cache=True,
            cache_durability=CacheDurabilityLevel.DURABLE_KV_SNAPSHOT,
            cache_scope="cross_process",
            supports_mid_generation_resume=True,
            requires_supervisor=True,
            requires_docker=False,
            requires_linux=False,
            notes="Owned AM Engine runtime with durable cross-process KV snapshots.",
        ),
        ProviderType.VLLM: BackendCapabilityProfile(
            provider_type=ProviderType.VLLM,
            modalities=("text",),
            substrates=("linux_native", "wsl_backend"),
            lifecycle_control="vllm-openai-server",
            load_unload_control=False,
            exclusive_gpu_lease=True,
            vram_accounting="vllm",
            prefix_cache=True,
            cache_durability=CacheDurabilityLevel.PREFIX_CACHE_REUSE,
            cache_scope="server",
            requires_linux=True,
            notes="OpenAI-compatible vLLM server tier; runtime health probes must not require importing vLLM.",
        ),
        ProviderType.NIM: BackendCapabilityProfile(
            provider_type=ProviderType.NIM,
            modalities=("text",),
            lifecycle_control="nim-openai-server",
            load_unload_control=False,
            exclusive_gpu_lease=True,
            vram_accounting="nim",
            cache_durability=CacheDurabilityLevel.PREFIX_CACHE_REUSE,
            cache_scope="server",
            requires_docker=True,
            notes="NVIDIA NIM OpenAI-compatible server tier; dry-run probes report declared cache posture only.",
        ),
        ProviderType.SGLANG: BackendCapabilityProfile(
            provider_type=ProviderType.SGLANG,
            modalities=("text",),
            substrates=("linux_native", "wsl_backend"),
            lifecycle_control="sglang-server",
            load_unload_control=False,
            exclusive_gpu_lease=True,
            vram_accounting="sglang",
            prefix_cache=True,
            cache_durability=CacheDurabilityLevel.PREFIX_CACHE_REUSE,
            cache_scope="server",
            requires_linux=True,
            notes="SGLang shared-prefix server tier; dry-run probes avoid importing optional backend packages.",
        ),
    }


def _media_backend_capabilities() -> dict[ProviderType, BackendCapabilityProfile]:
    """Return media and audio backend capability profiles."""
    return {
        ProviderType.COMFYUI: BackendCapabilityProfile(
            provider_type=ProviderType.COMFYUI,
            modalities=("image_generation", "video_generation", "three_d"),
            exclusive_gpu_lease=True,
            vram_accounting="diffusion",
            cache_durability=CacheDurabilityLevel.NONE,
            cache_scope="artifacts",
            supports_mid_generation_resume=True,
        ),
        ProviderType.FASTER_WHISPER: BackendCapabilityProfile(
            provider_type=ProviderType.FASTER_WHISPER,
            modalities=("audio_asr",),
            cache_durability=CacheDurabilityLevel.NONE,
            cache_scope="file_job",
        ),
    }


def _external_backend_capabilities() -> dict[ProviderType, BackendCapabilityProfile]:
    """Return external API backend capability profiles."""
    profiles = {
        ProviderType.OPENAI: BackendCapabilityProfile(
            provider_type=ProviderType.OPENAI,
            modalities=("text",),
            cache_durability=CacheDurabilityLevel.SEMANTIC_CACHE_ONLY,
            cache_scope="external",
        ),
    }
    for provider in [ProviderType.ANTHROPIC, ProviderType.GEMINI]:
        profiles[provider] = BackendCapabilityProfile(
            provider_type=provider,
            modalities=("text",),
            cache_durability=CacheDurabilityLevel.SEMANTIC_CACHE_ONLY,
            cache_scope="external",
        )
    return profiles


def default_backend_capabilities() -> dict[ProviderType, BackendCapabilityProfile]:
    """Return the seed capability matrix without importing backend packages.

    Returns:
        Mapping from provider type to its declared backend capability profile.
    """
    return {
        **_local_text_backend_capabilities(),
        **_media_backend_capabilities(),
        **_external_backend_capabilities(),
    }
