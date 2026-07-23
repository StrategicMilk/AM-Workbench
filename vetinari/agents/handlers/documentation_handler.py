"""Documentation mode handler for the Operations Agent.

Extracts the documentation generation logic from OperationsAgent into a
standalone handler class. Generates API reference docs, user guides,
changelogs, and README files with audience-aware formatting.
"""

from __future__ import annotations

import logging
from typing import Any

from vetinari.agents.contracts import AgentResult, AgentTask
from vetinari.agents.handlers import BaseHandler
from vetinari.boundary_guards import require_nonempty
from vetinari.constants import TRUNCATE_CONTENT_ANALYSIS

logger = logging.getLogger(__name__)


class DocumentationHandler(BaseHandler):
    """Handler for the 'documentation' mode of OperationsAgent.

    Generates structured documentation artifacts (API references, user guides,
    changelogs) by constructing a targeted prompt and delegating to the LLM
    inference callable provided in the execution context.
    """

    def __init__(self) -> None:
        super().__init__(
            mode_name="documentation",
            description="Generate API docs, user guides, changelogs, and README files",
        )

    def get_system_prompt(self) -> str:
        """Return the documentation-specialist system prompt.

        Returns:
            A multi-section prompt defining the documentation specialist's
            responsibilities, standards, quality checks, and output format.
        """
        return (
            "You are Vetinari's Documentation Specialist -- a technical writer with deep\n"
            "expertise in API documentation, developer experience, and information architecture.\n"
            "You produce documentation that is accurate, scannable, and immediately useful.\n\n"
            "## Core Responsibilities\n"
            "- Generate API reference documentation following OpenAPI/Swagger conventions\n"
            "- Write audience-aware user guides (beginner, intermediate, advanced tiers)\n"
            "- Produce changelogs following Keep a Changelog + Semantic Versioning\n"
            "- Create README files with Quick Start sections that get users running in <5 min\n"
            "- Write inline code comments and docstrings (Google/NumPy style)\n\n"
            "## Documentation Standards\n"
            "- Use imperative mood for instructions ('Install the package', not 'You should install')\n"
            "- Every public function/class documents: purpose, parameters, return value, exceptions, example\n"
            "- Include runnable code examples -- never pseudo-code in API docs\n"
            "- Structure with progressive disclosure: overview > quick start > detailed reference\n"
            "- Cross-reference related sections with relative links\n"
            "- Keep README files under 500 lines; link to /docs/ for deep content\n\n"
            "## Quality Checks\n"
            "- Verify all code examples are syntactically valid\n"
            "- Ensure parameter names match actual function signatures\n"
            "- Check that version numbers and paths are current\n"
            "- Flag undocumented public APIs as gaps\n"
            "- Validate Markdown renders correctly (no broken tables or links)\n\n"
            "## Output Format\n"
            "Return structured JSON with 'content' (full Markdown), 'type' (api_reference|guide|changelog),\n"
            "'sections' (array of {title, content}), and 'metadata' (audience, word_count, reading_time_min).\n"
            "Use clear, professional Markdown formatting with consistent heading hierarchy."
        )

    def handle(self, *, content: str, output_path: str) -> dict[str, str]:
        """Validate documentation content before any write path can proceed."""
        return {
            "content": require_nonempty(content, field_name="content"),
            "output_path": require_nonempty(output_path, field_name="output_path"),
        }

    def execute(self, task: AgentTask, context: dict[str, Any]) -> AgentResult:
        """Generate documentation for the given task.

        Constructs a documentation prompt from the task's context fields
        (content, doc_type, audience) and uses the 'infer_json' callable
        from the execution context to produce structured output.

        Args:
            task: The agent task containing the documentation request.
            context: Execution context; must contain an 'infer_json' callable
                with signature ``(prompt: str, fallback: Any) -> dict``.

        Returns:
            An AgentResult with the generated documentation as structured JSON
            in the output field.
        """
        content = task.context.get("content", task.description)
        doc_type = task.context.get("doc_type", "api_reference")
        audience = task.context.get("audience", "developers")
        require_nonempty(str(content), field_name="content")

        prompt = (
            f"Generate {doc_type} documentation for:\n{content[:TRUNCATE_CONTENT_ANALYSIS]}\n\n"
            f"Audience: {audience}\n\n"
            "Respond as JSON:\n"
            '{"content": "...markdown content...", "type": "' + doc_type + '", '
            '"sections": [{"title": "...", "content": "..."}], '
            '"metadata": {"audience": "' + audience + '", "word_count": 0}}'
        )

        fallback: dict[str, Any] = {"content": "", "type": doc_type, "sections": []}
        infer_json = context.get("infer_json")
        if infer_json is not None:
            result = infer_json(prompt, fallback=fallback)
            return AgentResult(
                success=True,
                output=result,
                metadata={"mode": "documentation", "doc_type": doc_type},
            )
        else:
            self._logger.warning("No infer_json callable in context, using fallback")
            return AgentResult(
                success=False,
                output=fallback,
                metadata={"mode": "documentation", "doc_type": doc_type, "_is_fallback": True},
            )
