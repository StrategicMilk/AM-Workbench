"""Canonical dependency and native-linkage contract for AM Engine bundles."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from types import MappingProxyType

CARGO_IDENTITY_COUNT = 221
CARGO_IDENTITY_SHA256 = "227cb5de92391b7ebbf9bf0e12b14e20f8f918853b14f24d1fe74689b8dffc4d"
CONVERTER_IDENTITY_COUNT_BY_PLATFORM = {"windows": 31, "linux": 30}
CONVERTER_IDENTITY_SHA256_BY_PLATFORM = {
    "windows": "d8f00a38e07f439dc94f703e1cf6d073bc91f8b858b1fd2e6bee7a12aaf4c094",
    "linux": "d57c7269365a8318e5215e530b76d8eb005a4ec7c4623b9ad07089e3c7a1f52c",
}
CUDA_DYNAMIC_EXTERNAL_LIBRARIES = ("cublas", "cublasLt", "cudart", "cuda")
CUDA_VERSION = "12.4.1"
CUDA_EULA_URL = "https://docs.nvidia.com/cuda/archive/12.4.1/pdf/EULA.pdf"
CUDA_EULA_SHA256 = "6ada441ae5a45a4a3d51ade8850fa2229fc6dd95de0c9da6e2cdd7a46701b844"

# Logical names are deliberately distinct from on-disk member names.  Consumers
# must select from this closed vocabulary instead of accepting an arbitrary
# relative path which could escape the verified installation.
EXPORT_TOOL_MEMBERS = MappingProxyType({
    "convert_hf_to_gguf": "convert_hf_to_gguf.py",
    "convert_lora_to_gguf": "convert_lora_to_gguf.py",
    "imatrix": "llama-imatrix",
    "quantize": "llama-quantize",
})
EXPORT_NATIVE_TOOLS = frozenset({"imatrix", "quantize"})


def export_tool_member(tool: str, *, platform: str) -> str:
    """Return the contracted bundle member for a logical export tool.

    Raises:
        ValueError: If the logical tool or platform is outside the immutable
            release vocabulary.
    """
    if platform not in {"linux", "windows"}:
        raise ValueError(f"unsupported AM Engine bundle platform: {platform!r}")
    try:
        member = EXPORT_TOOL_MEMBERS[tool]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"unsupported AM Engine export tool: {tool!r}") from exc
    if platform == "windows" and tool in EXPORT_NATIVE_TOOLS:
        return f"{member}.exe"
    return member


def export_tool_members(*, platform: str) -> frozenset[str]:
    """Return every export-tool member required for one bundle platform."""
    return frozenset(export_tool_member(tool, platform=platform) for tool in EXPORT_TOOL_MEMBERS)


def dependency_identity_digest(identities: Iterable[tuple[str, str]]) -> str:
    """Return the canonical SHA-256 for a dependency identity set.

    Args:
        identities: Package ``(name, version)`` pairs. Duplicate pairs are
            collapsed before hashing.

    Returns:
        Lowercase SHA-256 of sorted UTF-8 ``name==version`` lines.
    """
    canonical = "".join(f"{name}=={version}\n" for name, version in sorted(set(identities)))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
