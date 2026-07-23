"""Generation helpers for CodeAgentEngine."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from vetinari.adapters.base import InferenceRequest, InferenceResponse
from vetinari.constants import INFERENCE_STATUS_OK, MAX_TOKENS_CODE_GENERATION, MAX_TOKENS_REPO_MAP_CONTEXT
from vetinari.types import CodingTaskType

from .engine_models import CodeArtifact, CodingArtifactType, _CodeTask, _module_name_from_target

logger = logging.getLogger(__name__)


class CodeAgentGenerationMixin:
    """Generation behavior shared by the coding agent facade."""

    def _generate_via_llm(self, task: _CodeTask) -> CodeArtifact | None:
        """Generate code using the LLM via AdapterManager."""
        try:
            from vetinari.adapter_manager import get_adapter_manager

            adapter_manager = get_adapter_manager()
            if not adapter_manager.list_providers():
                logger.debug(
                    "No inference providers registered for coding task %s; using template fallback",
                    task.task_id,
                )
                return None

            target = task.target_files[0] if task.target_files else "output.py"
            user_prompt = self._coding_user_prompt(task, target)
            repo_map_context = self._repo_map_context(task)

            system_prompt = (
                f"You are an expert {task.language} developer. "
                f"Framework: {task.framework or 'none'}. "
                "Write clean, well-commented, production-quality code. "
                f"Follow best practices and handle edge cases.{repo_map_context}"
            )

            request = InferenceRequest(
                model_id="",
                prompt=user_prompt,
                system_prompt=system_prompt,
                max_tokens=MAX_TOKENS_CODE_GENERATION,
                metadata={
                    "agent": "coding_agent",
                    "task_id": task.task_id,
                    "task_type": task.type.value,
                },
            )
            response = adapter_manager.infer(request)
            content = self._extract_successful_llm_output(response, task)

            if not content:
                return None

            provenance = self._llm_artifact_provenance(response)
            return CodeArtifact(
                artifact_id=f"art_{uuid.uuid4().hex[:8]}",
                task_id=task.task_id,
                type=CodingArtifactType.FILE_CONTENTS,
                path=f"{task.repo_path}/{target}" if task.repo_path else target,
                content=content,
                provenance=provenance,
                language=task.language,
            )
        except Exception as e:
            logger.warning("LLM code generation failed; using scaffold fallback: %s", e)
            return None

    @staticmethod
    def _coding_user_prompt(task: _CodeTask, target: str) -> str:
        constraints_str = ", ".join(task.constraints) if isinstance(task.constraints, list) else task.constraints
        prompts = {
            CodingTaskType.SCAFFOLD: (
                f"Generate a complete {task.language} project scaffold for: {task.description}. "
                f"Framework: {task.framework or 'standard library'}. "
                f"Target file: {target}. Constraints: {constraints_str or 'none'}. "
                "Return ONLY the code, no explanations."
            ),
            CodingTaskType.IMPLEMENT: (
                f"Implement the following in {task.language}: {task.description}. "
                f"Target file: {target}. Constraints: {constraints_str or 'none'}. "
                "Return ONLY the complete implementation code."
            ),
            CodingTaskType.TEST: (
                f"Write comprehensive unit tests in {task.language} for: {task.description}. "
                f"Target: {target}. Use pytest. Return ONLY the test code."
            ),
            CodingTaskType.REVIEW: (
                f"Review this {task.language} code and provide actionable feedback: {task.description}. "
                "Return a structured review with: issues, improvements, security concerns, rating."
            ),
            CodingTaskType.REFACTOR: (
                f"Refactor the following {task.language} code to improve quality: {task.description}. "
                "Return ONLY the refactored code."
            ),
            CodingTaskType.FIX: (
                f"Fix the following bug in {task.language}: {task.description}. "
                f"Target: {target}. Return ONLY the fixed code."
            ),
            CodingTaskType.DOCUMENT: (
                f"Write documentation for: {task.description}. Return well-structured markdown documentation."
            ),
        }
        return prompts.get(task.type, f"Complete this coding task: {task.description}")

    @staticmethod
    def _repo_map_context(task: _CodeTask) -> str:
        try:
            from pathlib import Path as _Path

            from vetinari.repo_map import get_repo_map

            structure = get_repo_map().generate_for_task(
                root_path=_Path(task.repo_path) if task.repo_path else _Path.cwd(),
                task_description=task.description[:200],
                max_tokens=MAX_TOKENS_REPO_MAP_CONTEXT,
            )
            if structure:
                return f"\n\nCodebase structure:\n{structure}"
        except Exception:
            logger.warning("Repo map generation failed for task %s; proceeding without codebase context", task.task_id)
        return ""

    @staticmethod
    def _extract_successful_llm_output(response: InferenceResponse | dict[str, Any], task: _CodeTask) -> str:
        """Extract generated text only from successful AdapterManager responses.

        Args:
            response: Response returned by ``AdapterManager.infer``.
            task: Coding task used for diagnostic logging.

        Returns:
            Trimmed generated text, or an empty string when inference failed.
        """
        if isinstance(response, InferenceResponse):
            if response.status != INFERENCE_STATUS_OK:
                logger.warning(
                    "LLM code generation returned %s for task %s; using template fallback",
                    response.status,
                    task.task_id,
                )
                return ""
            return response.output.strip()

        status = response.get("status")
        if status and status != INFERENCE_STATUS_OK:
            logger.warning(
                "LLM code generation returned %s for task %s; using template fallback",
                status,
                task.task_id,
            )
            return ""
        output = response.get("output", "")
        return str(output).strip()

    @staticmethod
    def _llm_artifact_provenance(response: InferenceResponse | dict[str, Any]) -> str:
        """Build provenance that names the model and any evaluation receipt."""
        if isinstance(response, InferenceResponse):
            metadata = response.metadata or {}
            model_id = response.model_id or metadata.get("model_id") or "unknown"
            tokens_used = response.tokens_used
            status = response.status
        else:
            metadata_raw = response.get("metadata", {})
            metadata = metadata_raw if isinstance(metadata_raw, Mapping) else {}
            model_id = response.get("model_id") or metadata.get("model_id") or "unknown"
            tokens_used = response.get("tokens_used", "")
            status = response.get("status", "")

        eval_ref = (
            metadata.get("eval_ref")
            or metadata.get("quality_eval_ref")
            or metadata.get("quality_eval_id")
            or metadata.get("routing_receipt_id")
            or metadata.get("trace_eval_ref")
            or "missing"
        )
        provider = metadata.get("provider") or metadata.get("provider_name") or "unknown"
        return (
            "llm_generated"
            f";model_id={model_id}"
            f";provider={provider}"
            f";status={status or 'unknown'}"
            f";tokens_used={tokens_used}"
            f";eval_ref={eval_ref}"
        )

    def _generate_scaffold(self, task: _CodeTask) -> CodeArtifact:
        """Generate a Python package scaffold."""
        project_name = Path(task.target_files[0]).stem if task.target_files else "demo_project"

        scaffold_content = f'''"""
{project_name} - Auto-generated scaffold.
"""

__version__ = "0.1.0"
__author__ = "Vetinari Coding Agent"


class _ScaffoldLogger:
    def debug(self, *_args, **_kwargs):
        return None


logger = _ScaffoldLogger()


def main():
    """Main entry point."""
    logger.debug("Hello from {project_name}!")

if __name__ == "__main__":
    main()
'''

        artifact = CodeArtifact(
            artifact_id=f"art_{uuid.uuid4().hex[:8]}",
            task_id=task.task_id,
            type=CodingArtifactType.FILE_CONTENTS,
            path=f"{task.repo_path}/{project_name}/__init__.py" if task.repo_path else f"{project_name}/__init__.py",
            content=scaffold_content,
            provenance="in_process_coder",
            language=task.language,
        )

        logger.info("Generated scaffold for %s", project_name)
        return artifact

    @staticmethod
    def _generate_implementation(task: _CodeTask) -> CodeArtifact:
        """Generate implementation code."""
        target = task.target_files[0] if task.target_files else "module.py"

        impl_content = f'''"""
Implementation for {target}
Generated by Vetinari Coding Agent
"""

class Implementation:
    """Main implementation class."""

    def __init__(self):
        self.data = {{}}

    def process(self, input_data):
        """Process input data."""
        return {{
            "status": "success",
            "data": input_data
        }}
'''

        artifact = CodeArtifact(
            artifact_id=f"art_{uuid.uuid4().hex[:8]}",
            task_id=task.task_id,
            type=CodingArtifactType.FILE_CONTENTS,
            path=f"{task.repo_path}/{target}" if task.repo_path else target,
            content=impl_content,
            provenance="in_process_coder",
            language=task.language,
        )

        logger.info("Generated implementation for %s", target)
        return artifact

    def _generate_tests(self, task: _CodeTask) -> CodeArtifact:
        """Generate unit tests."""
        target = task.target_files[0] if task.target_files else "module"
        module_name = _module_name_from_target(target)
        test_content = f'''"""
Unit tests for {target}
Generated by Vetinari Coding Agent
"""

import importlib

import pytest


class TestImplementation:
    """Test cases for {target} Implementation class."""

    @pytest.fixture()
    def module_under_test(self):
        """Import the target module and fail when it is absent."""
        return importlib.import_module("{module_name}")

    def test_module_importable(self, module_under_test):
        """Verify the target module can be imported."""
        assert module_under_test is not None

    def test_implementation_instantiable(self, module_under_test):
        """Verify Implementation class can be instantiated."""
        impl_cls = getattr(module_under_test, "Implementation", None)
        assert impl_cls is not None, "Implementation class not found in {module_name}"
        impl = impl_cls()
        assert impl is not None

    def test_process_returns_dict(self, module_under_test):
        """Verify process() returns a dict when module is available."""
        impl_cls = getattr(module_under_test, "Implementation", None)
        assert impl_cls is not None, "Implementation class not found in {module_name}"
        impl = impl_cls()
        result = impl.process("test input")
        assert isinstance(result, dict)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
'''

        artifact = CodeArtifact(
            artifact_id=f"art_{uuid.uuid4().hex[:8]}",
            task_id=task.task_id,
            type=CodingArtifactType.TEST_ARTIFACT,
            path=f"{task.repo_path}/test_{target}.py" if task.repo_path else f"test_{target}.py",
            content=test_content,
            provenance="in_process_coder",
            language=task.language,
        )

        logger.info("Generated tests for %s", target)
        return artifact

    @staticmethod
    def _generate_review(task: _CodeTask) -> CodeArtifact:
        """Generate a code review summary."""
        review_content = f"""# Code Review Summary

## Task: {task.task_id}
## Type: {task.type.value}

### Files Reviewed
{task.target_files}

### Constraints
{", ".join(task.constraints) if isinstance(task.constraints, list) else task.constraints}

### Notes
- Code follows Python style guidelines
- Basic error handling implemented
- Tests should be added for edge cases
"""

        artifact = CodeArtifact(
            artifact_id=f"art_{uuid.uuid4().hex[:8]}",
            task_id=task.task_id,
            type=CodingArtifactType.FILE_CONTENTS,
            path=f"{task.repo_path}/review.md" if task.repo_path else "review.md",
            content=review_content,
            provenance="in_process_coder",
            language="markdown",
        )

        logger.info("Generated review for %s", task.task_id)
        return artifact

    @staticmethod
    def _generate_generic(task: _CodeTask) -> CodeArtifact:
        """Generate generic code for unspecified tasks."""
        header_lines = [
            "# Generated by Vetinari Coding Agent",
            f"# Task: {task.task_id}",
            f"# Type: {task.type.value}",
            f"# Language: {task.language}",
            "",
            task.description or "No description provided.",
        ]
        content = "\n".join(header_lines) + "\n"

        target = task.target_files[0] if task.target_files else "generated.py"

        return CodeArtifact(
            artifact_id=f"art_{uuid.uuid4().hex[:8]}",
            task_id=task.task_id,
            type=CodingArtifactType.FILE_CONTENTS,
            path=f"{task.repo_path}/{target}" if task.repo_path else target,
            content=content,
            provenance="in_process_coder",
            language=task.language,
        )
