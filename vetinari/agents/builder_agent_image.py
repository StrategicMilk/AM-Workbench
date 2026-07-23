"""Image generation helpers for BuilderAgent.

Contains the image generation mode extracted from ``builder_agent.py`` to keep
that file under the 550-line limit.

All public functions accept an ``agent: BuilderAgent`` as their first argument
and operate on its instance state (``_image_enabled``, ``_diffusion_engine``,
``_output_dir``, ``_steps``, ``_cfg_scale``, ``_default_width``,
``_default_height``).

Pipeline role: Build → **ImageGeneration** (Stable Diffusion / SVG fallback).
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vetinari.security.sandbox import enforce_blocked_paths

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from vetinari.agents.builder_agent import BuilderAgent


# Pre-compiled regex for extracting SVG content from LLM responses.
_SVG_EXTRACT_RE = re.compile(r"<svg[\s\S]*?</svg>", re.IGNORECASE)
_MIN_IMAGE_SIZE = 64
_MAX_IMAGE_SIZE = 2048
_MIN_STEPS = 1
_MAX_STEPS = 100
_MIN_CFG_SCALE = 1.0
_MAX_CFG_SCALE = 20.0


def bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    """Coerce an image integer option into an allowed range.

    Args:
        value: Candidate integer-like value.
        default: Fallback value when coercion fails.
        minimum: Lower inclusive bound.
        maximum: Upper inclusive bound.

    Returns:
        Parsed value clamped to the allowed range.
    """
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        logger.warning("Invalid integer image option %r; using default %d", value, default)
        return default
    return max(minimum, min(maximum, parsed))


def bounded_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    """Coerce an image float option into an allowed range.

    Args:
        value: Candidate float-like value.
        default: Fallback value when coercion fails.
        minimum: Lower inclusive bound.
        maximum: Upper inclusive bound.

    Returns:
        Parsed value clamped to the allowed range.
    """
    if isinstance(value, bool):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        logger.warning("Invalid float image option %r; using default %.3f", value, default)
        return default
    return max(minimum, min(maximum, parsed))


def sanitize_image_spec(agent: BuilderAgent, spec: dict[str, Any]) -> dict[str, Any]:
    """Clamp user-provided image generation options to supported bounds.

    Args:
        agent: Builder agent providing default image settings.
        spec: Mutable image specification to sanitize.

    Returns:
        The sanitized image specification.
    """
    width_default, height_default = get_default_size(str(spec.get("style_preset", "logo")))
    spec["width"] = bounded_int(spec.get("width"), width_default, _MIN_IMAGE_SIZE, _MAX_IMAGE_SIZE)
    spec["height"] = bounded_int(spec.get("height"), height_default, _MIN_IMAGE_SIZE, _MAX_IMAGE_SIZE)
    spec["steps"] = bounded_int(spec.get("steps"), agent._steps, _MIN_STEPS, _MAX_STEPS)
    spec["cfg_scale"] = bounded_float(spec.get("cfg_scale"), agent._cfg_scale, _MIN_CFG_SCALE, _MAX_CFG_SCALE)
    return spec


def execute_image_generation(agent: BuilderAgent, task: Any) -> Any:
    """Generate an image asset from the task description.

    Attempts Stable Diffusion generation via the in-process diffusion engine
    and falls back to an LLM-generated SVG when that is unavailable.

    Args:
        agent: The BuilderAgent instance whose config and services are used.
        task: The AgentTask containing description and context.

    Returns:
        AgentResult with generated image info or SVG fallback.
    """
    from vetinari.agents.contracts import AgentResult

    description = task.prompt or task.description
    context = task.context or {}

    image_spec = build_image_spec(agent, description, context)

    generated_images: list[dict[str, Any]] = []
    gen_error = None

    if agent._image_enabled:
        try:
            generated_images = generate_via_diffusers(agent, image_spec)
        except Exception as e:
            gen_error = str(e)
            logger.warning("Image generation failed: %s", e)

    if not generated_images:
        svg_code = image_spec.get("svg_fallback") or generate_svg_fallback(agent, description, image_spec)
        if svg_code:
            svg_path = save_svg(agent, svg_code, description)
            generated_images = [
                {
                    "type": "svg",
                    "path": str(svg_path),
                    "description": image_spec.get("description", description),
                    "code": svg_code,
                },
            ]

    backend = "diffusers" if (agent._image_enabled and not gen_error) else "svg_fallback"
    real_image_generated = (
        bool(generated_images)
        and backend == "diffusers"
        and any(image.get("type") != "svg" for image in generated_images)
    )

    result = {
        "images": generated_images,
        "spec": image_spec,
        "diffusers_available": agent._image_enabled and not gen_error,
        "gen_error": gen_error,
        "count": len(generated_images),
        "backend": backend,
        "is_diffusion_fallback": backend == "svg_fallback",
    }

    return AgentResult(
        success=real_image_generated,
        output=result,
        metadata={
            "mode": "image_generation",
            "image_count": len(generated_images),
            "backend": backend,
            "is_diffusion_fallback": backend == "svg_fallback",
            "description": description,
        },
    )


def build_image_spec(agent: BuilderAgent, description: str, context: dict[str, Any]) -> dict[str, Any]:
    """Use the LLM to build an optimized image specification.

    Args:
        agent: The BuilderAgent instance providing inference capability.
        description: Human-readable image description from the task.
        context: Task context dict with optional style/size hints.

    Returns:
        Specification dict with keys: sd_prompt, negative_prompt, style_preset,
        width, height, steps, description, and optionally svg_fallback.
    """
    style = detect_style(description)

    prompt = f"""Create an image generation specification for this asset:

Description: {description}
Context: {json.dumps(context, default=str)[:300] if context else "none"}

Infer the best style preset from: {", ".join(agent.STYLE_PRESETS.keys())}
For the SD prompt, use descriptive terms that produce professional results.
Include an SVG code fallback for simple icons/logos that can be represented as vectors.

Respond with valid JSON only."""

    spec = agent._infer_json(prompt) or {}

    if not spec.get("style_preset"):
        spec["style_preset"] = style
    if not spec.get("sd_prompt"):
        style_hint = agent.STYLE_PRESETS.get(style, "")
        spec["sd_prompt"] = f"{description}, {style_hint}"
    if not spec.get("negative_prompt"):
        spec["negative_prompt"] = agent.NEGATIVE_PROMPT
    if not spec.get("width"):
        spec["width"] = get_default_size(style)[0]
    if not spec.get("height"):
        spec["height"] = get_default_size(style)[1]
    if not spec.get("steps"):
        spec["steps"] = agent._steps
    if not spec.get("description"):
        spec["description"] = description

    return sanitize_image_spec(agent, spec)


def detect_style(description: str) -> str:
    """Detect appropriate style preset from description keywords.

    Args:
        description: Free-text image description from the task.

    Returns:
        One of the standard style preset keys: logo, icon, ui_mockup,
        diagram, banner, background, or logo as default.
    """
    desc_lower = description.lower()
    if any(kw in desc_lower for kw in ["logo", "brand", "identity", "company", "startup"]):
        return "logo"
    if any(kw in desc_lower for kw in ["icon", "button icon", "app icon", "fa icon"]):
        return "icon"
    if any(kw in desc_lower for kw in ["mockup", "wireframe", "ui", "layout", "screen"]):
        return "ui_mockup"
    if any(kw in desc_lower for kw in ["diagram", "flowchart", "architecture", "flow"]):
        return "diagram"
    if any(kw in desc_lower for kw in ["banner", "header image", "hero image"]):
        return "banner"
    if any(kw in desc_lower for kw in ["background", "texture", "wallpaper", "pattern"]):
        return "background"
    return "logo"


def get_default_size(style: str) -> tuple[int, int]:
    """Return default (width, height) in pixels for a style preset.

    Args:
        style: Style preset key.

    Returns:
        Tuple of (width, height) in pixels.
    """
    sizes: dict[str, tuple[int, int]] = {
        "logo": (512, 512),
        "icon": (256, 256),
        "ui_mockup": (1280, 720),
        "diagram": (1024, 768),
        "banner": (1200, 400),
        "background": (1920, 1080),
    }
    return sizes.get(style, (512, 512))


def get_diffusion_engine(agent: BuilderAgent) -> Any:
    """Lazy-initialize and return the diffusion engine.

    Caches the result on ``agent._diffusion_engine``. Returns None when
    the ``diffusers`` package is not installed or initialization fails.

    Args:
        agent: The BuilderAgent instance owning the engine slot.

    Returns:
        DiffusionEngine instance, or None if unavailable.
    """
    if agent._diffusion_engine is None:
        try:
            from vetinari.image.diffusion_engine import DiffusionEngine

            agent._diffusion_engine = DiffusionEngine(
                output_dir=str(agent._output_dir),
            )
            agent._diffusion_engine.discover_models()
        except ImportError:
            logger.debug("diffusers not installed — image generation unavailable")
            return None
        except Exception:
            logger.warning("Failed to initialize diffusion engine", exc_info=True)
            return None
    return agent._diffusion_engine


def generate_via_diffusers(agent: BuilderAgent, spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Generate images via the in-process diffusion engine.

    Args:
        agent: The BuilderAgent instance providing engine and config.
        spec: Image specification dict with prompt, dimensions, etc.

    Returns:
        List of generated image info dicts, or empty list if unavailable.
    """
    engine = get_diffusion_engine(agent)
    if engine is None or not engine.is_available():
        return []

    result = engine.generate(
        prompt=spec.get("sd_prompt", ""),
        negative_prompt=spec.get("negative_prompt", agent.NEGATIVE_PROMPT),
        width=bounded_int(spec.get("width"), agent._default_width, _MIN_IMAGE_SIZE, _MAX_IMAGE_SIZE),
        height=bounded_int(spec.get("height"), agent._default_height, _MIN_IMAGE_SIZE, _MAX_IMAGE_SIZE),
        steps=bounded_int(spec.get("steps"), agent._steps, _MIN_STEPS, _MAX_STEPS),
        cfg_scale=bounded_float(spec.get("cfg_scale"), agent._cfg_scale, _MIN_CFG_SCALE, _MAX_CFG_SCALE),
        seed=spec.get("seed", -1),
    )

    if not result.get("success"):
        logger.warning("Image generation failed: %s", result.get("error"))
        return []

    return [
        {
            "type": "png",
            "path": result["path"],
            "filename": result["filename"],
            "description": spec.get("description", ""),
            "prompt": spec.get("sd_prompt", ""),
        },
    ]


def generate_svg_fallback(agent: BuilderAgent, description: str, spec: dict[str, Any]) -> str:
    """Ask the LLM to generate SVG code as a fallback when diffusers are unavailable.

    Args:
        agent: The BuilderAgent instance providing inference.
        description: Human-readable image description.
        spec: Image spec dict providing style_preset.

    Returns:
        Valid SVG string, or a minimal placeholder SVG on failure.
    """
    from vetinari.exceptions import ModelUnavailableError

    style = spec.get("style_preset", "logo")
    size = get_default_size(style)

    prompt = f"""Generate a clean, professional SVG for the following:

Description: {description}
Style: {style}
Size: {size[0]}x{size[1]}

Requirements:
- Complete, valid SVG code only
- No external dependencies or scripts
- Clean vector shapes, appropriate for {style}
- Professional quality
- Include viewBox="0 0 {size[0]} {size[1]}"

Output ONLY the SVG code, starting with <svg and ending with </svg>."""

    try:
        result = agent._infer(prompt)
    except ModelUnavailableError:
        logger.warning("Model unavailable — returning minimal SVG placeholder for %s", description)
        return minimal_svg_placeholder(description, size)
    if not result:
        return minimal_svg_placeholder(description, size)

    svg_match = _SVG_EXTRACT_RE.search(result)
    if svg_match:
        return svg_match.group(0)

    if result.strip().startswith("<svg"):
        return result.strip()

    return minimal_svg_placeholder(description, size)


def minimal_svg_placeholder(description: str, size: tuple[int, int]) -> str:
    """Generate a descriptive placeholder SVG with keyword-based background color.

    Args:
        description: Human-readable description used to pick a thematic color.
        size: Tuple of (width, height) in pixels.

    Returns:
        Complete SVG string with colored background and description text.
    """
    w, h = size
    colors = {
        "landscape": "#4CAF50",
        "portrait": "#2196F3",
        "abstract": "#9C27B0",
        "diagram": "#FF9800",
        "chart": "#00BCD4",
        "icon": "#E91E63",
        "logo": "#3F51B5",
        "photo": "#795548",
    }
    bg_color = "#607D8B"
    for keyword, color in colors.items():
        if keyword in description.lower():
            bg_color = color
            break

    safe_desc = (
        description
        .replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    lines = [safe_desc[i : i + 40] for i in range(0, min(len(safe_desc), 120), 40)]
    text_elements = "\n".join(
        f'  <text x="{w // 2}" y="{h // 2 + i * 20 - len(lines) * 10}" '
        f'text-anchor="middle" fill="white" font-family="sans-serif" '
        f'font-size="13">{line}</text>'
        for i, line in enumerate(lines)
    )

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" '
        f'width="{w}" height="{h}" data-placeholder="true">\n'
        f'  <rect width="{w}" height="{h}" fill="{bg_color}" rx="8"/>\n'
        f'  <text x="{w // 2}" y="28" text-anchor="middle" fill="white" font-family="sans-serif"\n'
        f'        font-size="15" font-weight="bold">Image Placeholder ({w}x{h})</text>\n'
        f"{text_elements}\n"
        f"</svg>"
    )


def save_svg(agent: BuilderAgent, svg_code: str, description: str) -> Path:
    """Save SVG code to a timestamped file in the agent's output directory.

    Args:
        agent: The BuilderAgent instance providing the output directory.
        svg_code: Complete SVG markup to save.
        description: Human-readable description used only for logging.

    Returns:
        Path to the saved SVG file.
    """
    filename = f"img_{uuid.uuid4().hex[:8]}.svg"
    out_path = agent._output_dir / filename
    # Honour sandbox_policy.yaml blocked_paths even for agent-controlled output
    # directories — a misconfigured _output_dir pointing into a blocked tree
    # must still fail closed.
    enforce_blocked_paths(out_path)
    Path(out_path).write_text(svg_code, encoding="utf-8")
    logger.info("Generated SVG: %s", out_path)
    return out_path
