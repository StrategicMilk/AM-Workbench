"""Vetinari Builder Agent (v0.4.0).

Handles code scaffolding, generated-file writes, and image generation.
Delegates build, image, and prompt details to focused helper modules.
"""

from __future__ import annotations

import ast
import logging
import threading
from pathlib import Path
from typing import Any, cast

from vetinari.agents.builder_agent_image import (
    _MAX_CFG_SCALE,
    _MAX_IMAGE_SIZE,
    _MAX_STEPS,
    _MIN_CFG_SCALE,
    _MIN_IMAGE_SIZE,
    _MIN_STEPS,
)
from vetinari.agents.builder_agent_image import (
    bounded_float as _bounded_float,
)
from vetinari.agents.builder_agent_image import (
    bounded_int as _bounded_int,
)
from vetinari.agents.builder_agent_image import (
    build_image_spec as _build_image_spec_fn,
)
from vetinari.agents.builder_agent_image import (
    detect_style as _detect_style_fn,
)
from vetinari.agents.builder_agent_image import (
    execute_image_generation as _execute_image_generation_fn,
)
from vetinari.agents.builder_agent_image import (
    generate_svg_fallback as _generate_svg_fallback_fn,
)
from vetinari.agents.builder_agent_image import (
    generate_via_diffusers as _generate_via_diffusers_fn,
)
from vetinari.agents.builder_agent_image import (
    get_default_size as _get_default_size_fn,
)
from vetinari.agents.builder_agent_image import (
    get_diffusion_engine as _get_diffusion_engine_fn,
)
from vetinari.agents.builder_agent_image import (
    minimal_svg_placeholder as _minimal_svg_placeholder_fn,
)
from vetinari.agents.builder_agent_image import (
    save_svg as _save_svg_fn,
)
from vetinari.agents.builder_build import BuilderBuildBehavior
from vetinari.agents.builder_prompts import BUILDER_MODE_PROMPTS
from vetinari.agents.contracts import (
    AgentResult,
    AgentTask,
    VerificationResult,
)
from vetinari.agents.multi_mode_agent import MultiModeAgent
from vetinari.constants import (
    OUTPUTS_DIR,
    QUALITY_DEDUCTION_MISSING_ARTIFACTS,
    QUALITY_DEDUCTION_MISSING_CODE,
    QUALITY_DEDUCTION_MISSING_IMAGE,
    QUALITY_DEDUCTION_MISSING_NOTES,
    QUALITY_DEDUCTION_MISSING_SVG,
    QUALITY_DEDUCTION_MISSING_TESTS,
    TRUNCATE_CODE_ANALYSIS,
    TRUNCATE_OUTPUT_PREVIEW,
)
from vetinari.learning.atomic_writers import _write_text_atomic
from vetinari.sandbox_manager import get_sandbox_manager
from vetinari.security.sandbox import enforce_blocked_paths
from vetinari.types import AgentType

logger = logging.getLogger(__name__)


class BuilderAgent(BuilderBuildBehavior, MultiModeAgent):
    """Builder agent — code scaffolding, boilerplate, and image generation.

    Consolidates build and image-generation modes behind one Worker agent.
    """

    MODES = {
        "build": "_execute_build",
        "image_generation": "_execute_image_generation",
    }
    DEFAULT_MODE = "build"
    MODE_KEYWORDS = {
        "build": [
            "scaffold",
            "boilerplate",
            "generate code",
            "code gen",
            "project structure",
            "create project",
            "implement",
            "build",
            "code",
        ],
        "image_generation": [
            "image",
            "logo",
            "icon",
            "mockup",
            "diagram",
            "illustration",
            "banner",
            "background",
            "svg",
            "picture",
            "visual",
            "asset",
        ],
    }
    # -- Image generation constants --
    STYLE_PRESETS = {
        "logo": "clean vector logo, flat design, minimalist, professional, white background, "
        "high contrast, suitable for brand identity",
        "icon": "flat icon, simple shapes, clean lines, professional, vector style, 512x512, transparent background",
        "ui_mockup": "UI wireframe, clean layout, modern web design, annotated, low fidelity mockup",
        "diagram": "technical diagram, clean flowchart, professional infographic, clear labels, minimal color palette",
        "banner": "professional banner, marketing design, clean typography, modern gradient",
        "background": "abstract background, subtle texture, professional, suitable for UI, dark theme, minimal",
    }
    NEGATIVE_PROMPT = (
        "blurry, low quality, artifacts, watermark, text errors, distorted, "
        "ugly, deformed, bad anatomy, duplicate, extra elements, noisy, grainy"
    )

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(AgentType.WORKER, config)
        self._language = self._config.get("language", "python")
        self._image_enabled = bool(self._config.get("image_enabled", True))
        self._default_width = _bounded_int(self._config.get("width"), 1024, _MIN_IMAGE_SIZE, _MAX_IMAGE_SIZE)
        self._default_height = _bounded_int(self._config.get("height"), 1024, _MIN_IMAGE_SIZE, _MAX_IMAGE_SIZE)
        self._steps = _bounded_int(self._config.get("steps"), 20, _MIN_STEPS, _MAX_STEPS)
        self._cfg_scale = _bounded_float(self._config.get("cfg_scale"), 7.0, _MIN_CFG_SCALE, _MAX_CFG_SCALE)
        self._output_dir = Path(self._config.get("output_dir", str(OUTPUTS_DIR / "images")))
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._diffusion_engine: Any | None = None

    def _get_base_system_prompt(self) -> str:
        return (
            "You are Vetinari's Builder. You handle code scaffolding, boilerplate "
            "generation, test scaffolding, project structure creation, and visual "
            "asset generation (images, logos, icons, diagrams via Stable Diffusion "
            "or SVG fallback)."
        )

    def _get_mode_system_prompt(self, mode: str) -> str:
        """Return the LLM system prompt for the given Builder mode."""
        return str(BUILDER_MODE_PROMPTS.get(mode, ""))

    def verify(self, output: Any) -> VerificationResult:
        """Verify output is well-formed — mode-aware for build vs image.

        Returns:
            VerificationResult with a score reflecting completeness of the
            generated scaffold or image output.
        """
        if not isinstance(output, dict):
            return VerificationResult(passed=False, issues=[{"message": "Output must be a dict"}], score=0.0)

        # Image generation output
        images = output.get("images")
        if images is not None:
            if not images:
                return VerificationResult(passed=False, issues=[{"message": "No images generated"}], score=0.0)
            score = 1.0
            issues = []
            for img in images:
                if img.get("type") == "svg" and not img.get("code"):
                    issues.append({"message": f"SVG asset has no code: {img.get('description', '')}"})
                    score -= QUALITY_DEDUCTION_MISSING_SVG
                elif img.get("type") == "png" and (not img.get("path") or not Path(img["path"]).exists()):
                    issues.append({"message": f"Image file not found: {img.get('path', '')}"})
                    score -= QUALITY_DEDUCTION_MISSING_IMAGE
            return VerificationResult(passed=score >= 0.5, issues=issues, score=max(0.0, score))

        # Build output
        issues = []
        score = 1.0
        if not output.get("scaffold_code"):
            issues.append({"type": "missing_code", "message": "Scaffold code missing"})
            score -= QUALITY_DEDUCTION_MISSING_CODE
        if not output.get("tests"):
            issues.append({"type": "missing_tests", "message": "Test scaffolding missing"})
            score -= QUALITY_DEDUCTION_MISSING_TESTS
        if not output.get("artifacts"):
            issues.append({"type": "missing_artifacts", "message": "Artifact files missing"})
            score -= QUALITY_DEDUCTION_MISSING_ARTIFACTS
        if not output.get("implementation_notes"):
            issues.append({"type": "missing_notes", "message": "Implementation notes missing"})
            score -= QUALITY_DEDUCTION_MISSING_NOTES
        return VerificationResult(passed=score >= 0.5, issues=issues, score=max(0, score))

    def get_capabilities(self) -> list[str]:
        """Return capability strings describing this agent's supported modes and features.

        Returns:
            List of capability identifiers such as code scaffolding,
            test generation, and image generation.
        """
        return [
            "code_scaffolding",
            "test_generation",
            "boilerplate_creation",
            "project_structure",
            "configuration_templates",
            "documentation_generation",
            "image_generation",
            "logo_design",
            "icon_creation",
            "ui_mockup",
            "diagram_generation",
            "svg_generation",
            "asset_creation",
        ]

    # -- Tool-aware file writing --

    def _tool_write_file(self, path: str, content: str) -> bool:
        """Write a file via the file_operations tool (auditable, permission-gated).

        Falls back to direct I/O only when the tool is unavailable. If the
        auditable tool is present but fails, direct writes remain blocked.

        Args:
            path: Destination file path as a string.
            content: Text content to write.

        Returns:
            True if the file was written successfully, False otherwise.
        """
        if self._has_tool("file_operations"):
            result = self._use_tool("file_operations", operation="write", path=path, content=content)
            if result and result.get("success"):
                return True
            logger.warning("file_operations write failed for path %s; direct fallback blocked", path)
            return False

        try:
            p = Path(path)
            # Even on the direct-write fallback (file_operations tool unavailable)
            # we MUST consult sandbox blocked_paths — a misconfigured target
            # must not silently bypass the policy just because the audited tool
            # is missing.
            enforce_blocked_paths(p)
            p.parent.mkdir(parents=True, exist_ok=True)
            _write_text_atomic(p, content)
            return True
        except Exception as exc:
            logger.warning(
                "Direct file write failed for path %s: %s — file not written, caller should treat as write failure",
                path,
                exc,
            )
            return False

    @staticmethod
    def _resolve_generated_output_path(base: Path, filename: str) -> Path | None:
        candidate = Path(filename)
        if candidate.is_absolute():
            logger.warning("[BuilderAgent] Rejected absolute generated filename: %s", filename)
            return None
        try:
            base_resolved = base.resolve()
            resolved = (base / candidate).resolve()
        except OSError as exc:
            logger.warning("[BuilderAgent] Rejected generated filename %s: %s", filename, exc)
            return None
        if not resolved.is_relative_to(base_resolved):
            logger.warning("[BuilderAgent] Rejected generated filename outside output dir: %s", filename)
            return None
        return resolved

    def _write_scaffold_to_disk(self, scaffold: dict[str, Any], output_dir: str) -> list[str]:
        """Write all scaffold files (code, tests, artifacts) to output_dir.

        Args:
            scaffold: Scaffold dict with scaffold_code, tests, and artifacts.
            output_dir: Directory to write files into. Created if absent.

        Returns:
            List of file paths that were successfully written.
        """
        written: list[str] = []
        base = Path(output_dir)
        base.mkdir(parents=True, exist_ok=True)

        code = scaffold.get("scaffold_code", "")
        if code:
            feature_name = scaffold.get("summary", "feature").split()[-1]
            safe_name = "".join(c if c.isalnum() or c == "_" else "_" for c in feature_name.lower())
            code_path = str(base / f"{safe_name}.py")
            if self._tool_write_file(code_path, code):
                written.append(code_path)
                logger.info("[BuilderAgent] Wrote %s", code_path)

        for test in scaffold.get("tests", []):
            fname = test.get("filename", "test_generated.py")
            content = test.get("content", "")
            if content:
                test_path = self._resolve_generated_output_path(base, fname)
                if test_path and self._tool_write_file(str(test_path), content):
                    written.append(str(test_path))

        for artifact in scaffold.get("artifacts", []):
            fname = artifact.get("filename", "artifact.txt")
            content = artifact.get("content", "")
            if content:
                artifact_path = self._resolve_generated_output_path(base, fname)
                if artifact_path and self._tool_write_file(str(artifact_path), content):
                    written.append(str(artifact_path))

        return written

    @staticmethod
    def _check_syntax(code: str) -> list[str]:
        """Return list of syntax error messages for Python code, or empty list.

        Args:
            code: Python source code to check.

        Returns:
            List of error message strings, or empty list when the code is valid.
        """
        if not code or not code.strip():
            return []
        try:
            ast.parse(code)
            return []
        except SyntaxError as e:
            logger.warning("Syntax error in generated code at line %s: %s — flagging for repair", e.lineno, e.msg)
            return [f"SyntaxError at line {e.lineno}: {e.msg}"]
        except Exception as e:
            logger.warning(
                "AST parse failed with unexpected error during syntax check: %s — treating as syntax error",
                e,
            )
            return [str(e)]

    def write_and_execute(
        self,
        code: str,
        timeout: int = 30,
        working_dir: str | None = None,
    ) -> dict[str, Any]:
        """Execute generated Python code through the canonical sandbox manager.

        Args:
            code: Python source code to execute.
            timeout: Maximum seconds to wait for the subprocess.
            working_dir: Ignored; sandbox-managed execution owns its working dir.

        Returns:
            Dict with keys: ``success`` (bool), ``stdout``, ``stderr``,
            ``returncode``, and ``syntax_errors`` (list). ``success`` is False
            when syntax checking fails, execution times out, or the process
            exits non-zero.
        """
        syntax_errors = self._check_syntax(code)
        if syntax_errors:
            return {
                "success": False,
                "stdout": "",
                "stderr": "\n".join(syntax_errors),
                "returncode": -1,
                "syntax_errors": syntax_errors,
            }

        if working_dir is not None:
            logger.info("write_and_execute ignores working_dir because execution is sandbox-managed")

        try:
            result = get_sandbox_manager().execute(
                code=code,
                sandbox_type="subprocess",
                timeout=timeout,
                context={},
                client_id="builder.write_and_execute",
            )
            return {
                "success": result.success,
                "stdout": (result.result or "")[:TRUNCATE_CODE_ANALYSIS],
                "stderr": (result.error or "")[:TRUNCATE_OUTPUT_PREVIEW],
                "returncode": 0 if result.success else -1,
                "syntax_errors": [],
            }
        except Exception as e:
            logger.warning(
                "write_and_execute failed through SandboxManager: %s — returning failure, code output not available",
                e,
            )
            return {
                "success": False,
                "stdout": "",
                "stderr": str(e),
                "returncode": -1,
                "syntax_errors": [],
            }

    # -- Image generation mode — delegates to builder_agent_image.py --

    def _execute_image_generation(self, task: AgentTask) -> AgentResult:
        """Generate an image asset from the task description.

        Delegates to ``builder_agent_image.execute_image_generation`` which
        handles Stable Diffusion and SVG fallback generation.

        Args:
            task: AgentTask with description and context for the image.

        Returns:
            AgentResult with generated image info or SVG fallback.
        """
        return _execute_image_generation_fn(self, task)

    def _build_image_spec(self, description: str, context: dict[str, Any]) -> dict[str, Any]:
        """Use the LLM to build an optimized image specification.

        Delegates to ``builder_agent_image.build_image_spec``.

        Args:
            description: Human-readable image description.
            context: Task context dict.

        Returns:
            Specification dict with sd_prompt, style_preset, dimensions, etc.
        """
        return cast(dict[str, Any], _build_image_spec_fn(self, description, context))

    # Thin wrappers so subclasses and tests can override methods individually.

    @staticmethod
    def _detect_style(description: str) -> str:
        """Detect the appropriate style preset from the image description.

        Args:
            description: Free-text image description.

        Returns:
            Style preset key such as logo, icon, diagram, banner, or background.
        """
        return str(_detect_style_fn(description))

    @staticmethod
    def _get_default_size(style: str) -> tuple[int, int]:
        """Return default (width, height) pixels for a style preset.

        Args:
            style: Style preset key.

        Returns:
            Tuple of (width, height) integers.
        """
        return cast(tuple[int, int], _get_default_size_fn(style))

    def _get_diffusion_engine(self) -> Any:
        """Lazy-initialize the diffusion engine and return it.

        Returns:
            DiffusionEngine instance, or None if unavailable.
        """
        return _get_diffusion_engine_fn(self)

    def _generate_via_diffusers(self, spec: dict[str, Any]) -> list[dict[str, Any]]:
        """Generate images via the in-process diffusion engine.

        Args:
            spec: Image specification dict with prompt, dimensions, etc.

        Returns:
            List of generated image info dicts, or empty list if unavailable.
        """
        return cast(list[dict[str, Any]], _generate_via_diffusers_fn(self, spec))

    def _generate_svg_fallback(self, description: str, spec: dict[str, Any]) -> str:
        """Ask the LLM to generate SVG code as a fallback.

        Args:
            description: Human-readable image description.
            spec: Image spec dict providing style_preset.

        Returns:
            Valid SVG string, or a minimal placeholder SVG on failure.
        """
        return str(_generate_svg_fallback_fn(self, description, spec))

    @staticmethod
    def _minimal_svg_placeholder(description: str, size: tuple[int, int]) -> str:
        """Generate a descriptive placeholder SVG with keyword-based theming.

        Args:
            description: Description used to pick a thematic background color.
            size: Tuple of (width, height) in pixels.

        Returns:
            Complete SVG string with colored background and description text.
        """
        return str(_minimal_svg_placeholder_fn(description, size))

    def _save_svg(self, svg_code: str, description: str) -> Path:
        """Save SVG code to the agent's output directory.

        Args:
            svg_code: Complete SVG markup to save.
            description: Description used for log messages.

        Returns:
            Path to the saved SVG file.
        """
        return cast(Path, _save_svg_fn(self, svg_code, description))


# -- Singleton --
_builder_agent: BuilderAgent | None = None
_builder_agent_lock = threading.Lock()


def get_builder_agent(config: dict[str, Any] | None = None) -> BuilderAgent:
    """Get or create the singleton BuilderAgent instance.

    Uses double-checked locking to prevent race conditions on first call.

    Args:
        config: Optional configuration overrides for the agent.

    Returns:
        The shared BuilderAgent instance.
    """
    global _builder_agent
    if _builder_agent is None:
        with _builder_agent_lock:
            if _builder_agent is None:
                _builder_agent = BuilderAgent(config)
    return _builder_agent
