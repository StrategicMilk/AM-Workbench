"""In-process image generation engine using HuggingFace diffusers.

Replaces the external Stable Diffusion WebUI dependency with embedded
inference via the diffusers library. Supports SDXL, FLUX, and other
diffusion models that can be loaded from local .safetensors files or
HuggingFace-format model directories.

Features:
- Auto-discovery of local diffusion models
- Lazy model loading with VRAM management
- GPU acceleration with torch.compile() support
- CPU offload for models exceeding VRAM budget
- PNG output to configurable directory
"""

from __future__ import annotations

import gc
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from vetinari.constants import _PROJECT_ROOT
from vetinari.utils.lazy_import import lazy_import

logger = logging.getLogger(__name__)


# ── Optional imports ─────────────────────────────────────────────────────────
torch: Any
diffusers: Any
PILImage: Any
torch, _TORCH_AVAILABLE = lazy_import("torch")
diffusers, _DIFFUSERS_AVAILABLE = lazy_import("diffusers")
PILImage, _PIL_AVAILABLE = lazy_import("PIL.Image")


# ── Configuration defaults ───────────────────────────────────────────────────
DEFAULT_IMAGE_MODELS_DIR = os.environ.get("VETINARI_IMAGE_MODELS_DIR", str(_PROJECT_ROOT / "image_models"))
DEFAULT_IMAGE_STEPS = int(os.environ.get("VETINARI_IMAGE_STEPS", "20"))
DEFAULT_IMAGE_CFG = float(os.environ.get("VETINARI_IMAGE_CFG", "7.0"))
DEFAULT_IMAGE_WIDTH = int(os.environ.get("VETINARI_IMAGE_WIDTH", "1024"))
DEFAULT_IMAGE_HEIGHT = int(os.environ.get("VETINARI_IMAGE_HEIGHT", "1024"))
DEFAULT_OUTPUT_DIR = os.environ.get("VETINARI_IMAGE_OUTPUT_DIR", str(_PROJECT_ROOT / "outputs" / "images"))


# ── Model detection patterns ─────────────────────────────────────────────────
_MODEL_PATTERNS: list[tuple[str, str]] = [
    (r"flux", "flux"),
    (r"sdxl|stable.diffusion.xl", "sdxl"),
    (r"sd3|stable.diffusion.3", "sd3"),
    (r"stable.diffusion|sd.?1\.5|sd.?2\.1", "sd15"),
]


def _detect_model_type(model_path: Path) -> str:
    """Detect the diffusion model type from its directory or filename.

    Args:
        model_path: Path to the model directory or file.

    Returns:
        Model type string: 'sdxl', 'flux', 'sd3', 'sd15', or 'auto'.
    """
    import re

    name = model_path.name.lower()
    for pattern, model_type in _MODEL_PATTERNS:
        if re.search(pattern, name):
            return model_type
    return "auto"


class DiffusionEngine:
    """In-process image generation engine using HuggingFace diffusers.

    Loads diffusion models from local directories and generates images
    in-process on the GPU. No external server required.

    Usage::

        engine = DiffusionEngine(models_dir="./image_models")
        models = engine.discover_models()
        image_path = engine.generate(
            prompt="a professional logo for a tech company",
            width=1024,
            height=1024,
        )
    """

    def __init__(
        self,
        models_dir: str | Path | None = None,
        output_dir: str | Path | None = None,
        device: str = "cuda",
        dtype: str = "float16",
        compile_model: bool = False,
    ):
        """Configure model storage paths, hardware device, and dtype for subsequent image generation.

        Args:
            models_dir: Directory containing diffusion model directories.
            output_dir: Directory to save generated images.
            device: Torch device ('cuda', 'cpu', 'auto').
            dtype: Data type ('float16', 'bfloat16', 'float32').
            compile_model: Enable torch.compile() for faster inference.
        """
        self._models_dir = Path(models_dir or DEFAULT_IMAGE_MODELS_DIR)
        self._output_dir = Path(output_dir or DEFAULT_OUTPUT_DIR)
        try:
            self._output_dir.mkdir(parents=True, exist_ok=True)
            self._output_dir_writable = True
        except (PermissionError, OSError):
            logger.warning(
                "Image output directory %s is not writable — image generation disabled",
                self._output_dir,
            )
            self._output_dir_writable = False

        self._device = device
        self._dtype_str = dtype
        self._compile = compile_model

        self._pipeline: Any | None = None
        self._loaded_model_id: str | None = None
        self._discovered_models: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    @property
    def _torch_dtype(self) -> Any:
        """Resolve string dtype to torch dtype object.

        Returns:
            The torch dtype, or None if torch is unavailable.
        """
        if not _TORCH_AVAILABLE:
            return None
        dtype_map = {
            "float16": torch.float16,
            "bfloat16": torch.bfloat16,
            "float32": torch.float32,
        }
        return dtype_map.get(self._dtype_str, torch.float16)

    def is_available(self) -> bool:
        """Check if diffusers and torch are installed and GPU is available.

        Returns:
            True if image generation is possible.
        """
        if not _DIFFUSERS_AVAILABLE or not _TORCH_AVAILABLE or not _PIL_AVAILABLE:
            return False
        return not (self._device == "cuda" and not torch.cuda.is_available())

    def has_models(self) -> bool:
        """Check if at least one model exists in the configured models directory.

        Returns:
            True if discover_models() returns a non-empty list.
        """
        return len(self.discover_models()) > 0

    def discover_models(self) -> list[dict[str, Any]]:
        """Scan models directory for diffusion models.

        Looks for directories containing model_index.json (HuggingFace format)
        or .safetensors files.

        Returns:
            List of model info dicts with id, name, type, path.
        """
        models: list[dict[str, Any]] = []

        if not self._models_dir.exists():
            logger.warning("Image models directory does not exist: %s", self._models_dir)
            return models

        # Look for HuggingFace-format model directories (have model_index.json)
        # Use depth-1 glob to avoid matching component dirs like mymodel/unet/model_index.json
        for model_index in self._models_dir.glob("*/model_index.json"):
            model_dir = model_index.parent
            model_id = model_dir.name
            model_type = _detect_model_type(model_dir)
            models.append({
                "id": model_id,
                "name": model_id,
                "type": model_type,
                "path": str(model_dir),
                "format": "diffusers",
            })
            logger.info("Discovered diffusion model: %s (type=%s)", model_id, model_type)

        # Collect HF model dirs to exclude their internal component files
        hf_model_dirs = [Path(m["path"]) for m in models]

        # Look for single-file safetensors — any depth, but not inside HF dirs
        for safetensors_file in self._models_dir.rglob("*.safetensors"):
            if any(safetensors_file.is_relative_to(hf_dir) for hf_dir in hf_model_dirs):
                continue  # part of a HF model dir, not a standalone checkpoint
            model_id = safetensors_file.stem
            model_type = _detect_model_type(safetensors_file)
            models.append({
                "id": model_id,
                "name": model_id,
                "type": model_type,
                "path": str(safetensors_file),
                "format": "safetensors",
            })
            logger.info("Discovered safetensors model: %s (type=%s)", model_id, model_type)

        self._discovered_models = models
        return models

    def generate(
        self,
        prompt: str,
        negative_prompt: str = "",
        width: int = DEFAULT_IMAGE_WIDTH,
        height: int = DEFAULT_IMAGE_HEIGHT,
        steps: int = DEFAULT_IMAGE_STEPS,
        cfg_scale: float = DEFAULT_IMAGE_CFG,
        seed: int = -1,
        model_id: str | None = None,
    ) -> dict[str, Any]:
        """Generate an image from a text prompt.

        Args:
            prompt: Text description of the image to generate.
            negative_prompt: What to avoid in the image.
            width: Image width in pixels.
            height: Image height in pixels.
            steps: Number of inference steps.
            cfg_scale: Classifier-free guidance scale.
            seed: Random seed (-1 for random).
            model_id: Specific model to use (None for first available).

        Returns:
            Dict with keys: success, path, filename, width, height, error.

        Raises:
            RuntimeError: If diffusers/torch are not installed.
        """
        if not self.is_available():
            return {
                "success": False,
                "path": "",
                "filename": "",
                "error": "diffusers, torch, or Pillow not installed (pip install vetinari[image])",
            }

        if not self._output_dir_writable:
            return {
                "success": False,
                "path": "",
                "filename": "",
                "error": f"Image output directory {self._output_dir} is not writable",
            }

        prompt_error = self._validate_generation_inputs(prompt, width, height, steps, cfg_scale)
        if prompt_error is not None:
            return prompt_error

        with self._lock:
            try:
                return self._generate_locked(prompt, negative_prompt, width, height, steps, cfg_scale, seed, model_id)

            except Exception as exc:
                logger.error("Image generation failed: %s", exc)
                return {
                    "success": False,
                    "path": "",
                    "filename": "",
                    "error": f"Generation failed: {exc}",
                }

    @staticmethod
    def _validate_generation_inputs(
        prompt: str,
        width: int,
        height: int,
        steps: int,
        cfg_scale: float,
    ) -> dict[str, Any] | None:
        if not prompt or not prompt.strip():
            return {"success": False, "path": "", "filename": "", "error": "Prompt cannot be empty"}
        if not isinstance(width, int) or not (64 <= width <= 4096) or width % 8 != 0:
            raise ValueError(f"Width must be a positive integer in range 64-4096 and divisible by 8, got {width!r}")
        if not isinstance(height, int) or not (64 <= height <= 4096) or height % 8 != 0:
            raise ValueError(f"Height must be a positive integer in range 64-4096 and divisible by 8, got {height!r}")
        if not isinstance(steps, int) or not (1 <= steps <= 200):
            raise ValueError(f"Steps must be a positive integer in range 1-200, got {steps!r}")
        if not isinstance(cfg_scale, (int, float)) or not (1.0 <= float(cfg_scale) <= 30.0):
            raise ValueError(f"cfg_scale must be a positive float in range 1.0-30.0, got {cfg_scale!r}")
        return None

    def _generate_locked(
        self,
        prompt: str,
        negative_prompt: str,
        width: int,
        height: int,
        steps: int,
        cfg_scale: float,
        seed: int,
        model_id: str | None,
    ) -> dict[str, Any]:
        pipeline = self._get_or_load_pipeline(model_id)
        generator = torch.Generator(device=self._device).manual_seed(seed) if seed >= 0 else None
        start_time = time.time()
        result = pipeline(
            prompt=prompt,
            negative_prompt=negative_prompt or None,
            width=width,
            height=height,
            num_inference_steps=steps,
            guidance_scale=cfg_scale,
            generator=generator,
        )
        elapsed_ms = int((time.time() - start_time) * 1000)
        filename = f"img_{uuid.uuid4().hex[:8]}.png"
        out_path = self._output_dir / filename
        result.images[0].save(str(out_path))
        logger.info("Generated image: %s (%dx%d, %d steps, %d ms)", out_path, width, height, steps, elapsed_ms)
        return {
            "success": True,
            "path": str(out_path),
            "filename": filename,
            "width": width,
            "height": height,
            "elapsed_ms": elapsed_ms,
            "error": "",
        }

    def _get_or_load_pipeline(self, model_id: str | None = None) -> Any:
        """Load a diffusion pipeline, reusing cached if same model.

        Args:
            model_id: Model to load. None uses first discovered model.

        Returns:
            A diffusers pipeline ready for inference.

        Raises:
            RuntimeError: If no models are available.
        """
        # Always rediscover to pick up newly added or removed models
        self.discover_models()

        if not self._discovered_models:
            msg = f"No diffusion models found in {self._models_dir}"
            raise RuntimeError(msg)

        # Find the requested model
        if model_id:
            for m in self._discovered_models:
                if m["id"] == model_id:
                    target = m
                    break
            else:
                available = [m["id"] for m in self._discovered_models]
                msg = f"Model '{model_id}' not found. Available models: {available}"
                raise ValueError(msg)
        else:
            target = self._discovered_models[0]  # default to first

        # Return cached pipeline if same model
        if self._pipeline is not None and self._loaded_model_id == target["id"]:
            return self._pipeline

        # Unload previous pipeline
        self._unload_pipeline()

        # Load new pipeline
        model_path = target["path"]
        model_type = target["type"]
        logger.info("Loading diffusion model: %s (type=%s)", target["id"], model_type)

        if target.get("format") == "safetensors":
            pipeline = diffusers.AutoPipelineForText2Image.from_single_file(
                model_path,
                torch_dtype=self._torch_dtype,
            )
        else:
            pipeline = diffusers.AutoPipelineForText2Image.from_pretrained(
                model_path,
                torch_dtype=self._torch_dtype,
                use_safetensors=True,
                local_files_only=True,
            )

        # Move to GPU with memory optimization
        if self._device == "cuda":
            try:
                pipeline.enable_model_cpu_offload()
            except Exception:
                logger.warning("CPU offload not supported, moving to device directly")
                pipeline = pipeline.to(self._device)
        else:
            pipeline = pipeline.to(self._device)

        # Optional torch.compile for 2x speedup
        if self._compile and hasattr(pipeline, "unet"):
            try:
                pipeline.unet = torch.compile(pipeline.unet, mode="reduce-overhead")
                logger.info("Applied torch.compile to UNet")
            except Exception:
                logger.warning("torch.compile not available or failed", exc_info=True)

        self._pipeline = pipeline
        self._loaded_model_id = target["id"]
        return pipeline

    def _unload_pipeline(self) -> None:
        """Unload the current pipeline and free VRAM."""
        if self._pipeline is not None:
            logger.info("Unloading diffusion model: %s", self._loaded_model_id)
            del self._pipeline
            self._pipeline = None
            self._loaded_model_id = None
            if _TORCH_AVAILABLE and torch.cuda.is_available():
                torch.cuda.empty_cache()
            gc.collect()

    def get_loaded_model(self) -> str | None:
        """Return the ID of the currently loaded model, or None.

        Returns:
            Model ID string or None.
        """
        return self._loaded_model_id
