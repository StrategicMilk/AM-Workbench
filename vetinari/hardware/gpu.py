"""GPU detection helpers."""

from __future__ import annotations

import logging
import subprocess
from typing import Any

logger = logging.getLogger(__name__)


def detect_gpu() -> dict[str, Any]:
    """Detect the active GPU.

    Returns:
        GPU information mapping.
    """
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],  # noqa: S607 - standard PATH driver tool, fixed args, no shell/user input
            capture_output=True,
            check=True,
            text=True,
            timeout=2,
        )
        first_line = next((line.strip() for line in result.stdout.splitlines() if line.strip()), "")
        if first_line:
            name, _, memory_mb = first_line.partition(",")
            try:
                vram_gb = round(float(memory_mb.strip()) / 1024, 3)
            except ValueError:
                vram_gb = None
            return {"type": name.strip() or "nvidia", "vram_gb": vram_gb, "source": "nvidia-smi"}
    except (FileNotFoundError, subprocess.SubprocessError, OSError) as exc:
        logger.warning("nvidia-smi GPU probe failed: %s", exc)

    try:
        import torch  # type: ignore[import-not-found]

        if torch.cuda.is_available():
            device_index = torch.cuda.current_device()
            props = torch.cuda.get_device_properties(device_index)
            return {
                "type": props.name,
                "vram_gb": round(float(props.total_memory) / (1024**3), 3),
                "source": "torch",
            }
    except Exception as exc:
        logger.warning("torch GPU probe failed: %s", exc)

    return {"type": "unknown", "vram_gb": None}


def parse_gpu_info(gpu_info: dict[str, Any]) -> dict[str, Any]:
    """Parse GPU information.

    Args:
        gpu_info: Raw GPU information mapping.

    Returns:
        Parsed GPU information mapping.
    """
    parsed = dict(gpu_info)
    raw_vram = parsed.get("vram_raw")
    try:
        parsed["vram_gb"] = float(raw_vram) if raw_vram is not None else parsed.get("vram_gb")
    except (TypeError, ValueError):
        parsed["vram_gb"] = None
    return parsed


__all__ = ["detect_gpu", "parse_gpu_info"]
