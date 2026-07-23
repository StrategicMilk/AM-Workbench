"""Creative writing mode handler for the Operations Agent.

Extracts the creative writing / content generation logic from
OperationsAgent into a standalone handler class. Produces release
announcements, blog posts, project narratives, and marketing copy.
"""

from __future__ import annotations

import logging
from typing import Any

from vetinari.agents.contracts import AgentResult, AgentTask
from vetinari.agents.handlers import BaseHandler
from vetinari.constants import TRUNCATE_CONTENT_ANALYSIS

logger = logging.getLogger(__name__)


class CreativeWritingHandler(BaseHandler):
    """Handler for the 'creative_writing' mode of OperationsAgent.

    Generates creative content (blog posts, release announcements, narratives)
    by constructing a style-aware prompt and delegating to the LLM inference
    callable provided in the execution context.
    """

    def __init__(self) -> None:
        super().__init__(
            mode_name="creative_writing",
            description="Generate creative content: blog posts, announcements, narratives",
        )

    def get_system_prompt(self) -> str:
        """Return the creative-writing specialist system prompt.

        Returns:
            A multi-section prompt defining the creative writer's
            responsibilities, principles, style guidelines, and output format.
        """
        return (
            "You are Vetinari's Creative Content Specialist -- a versatile writer capable of\n"
            "producing engaging, well-structured content across multiple formats and styles.\n"
            "You balance creativity with clarity, and voice with purpose.\n\n"
            "## Core Responsibilities\n"
            "- Generate release announcements, blog posts, and marketing copy for technical products\n"
            "- Write project narratives and case studies that explain complex systems accessibly\n"
            "- Produce onboarding content, tutorials, and explanatory articles\n"
            "- Create internal communications (team updates, milestone summaries, retrospectives)\n\n"
            "## Writing Principles\n"
            "- Match tone to audience: formal for enterprise, conversational for developer blogs\n"
            "- Lead with the value proposition -- what does the reader gain?\n"
            "- Use concrete examples over abstract claims ('3x faster' > 'significantly improved')\n"
            "- Structure with scannable headings, bullet points, and short paragraphs\n"
            "- End with a clear call-to-action or next step\n\n"
            "## Style Guidelines\n"
            "- Active voice preferred; passive only when the actor is irrelevant\n"
            "- Avoid jargon unless the audience expects it, then define on first use\n"
            "- One idea per paragraph, one purpose per section\n"
            "- Use analogies to bridge unfamiliar concepts to familiar ones\n"
            "- Vary sentence length for rhythm -- mix short punchy sentences with longer explanatory ones\n\n"
            "## Output Format\n"
            "Return JSON with 'content' (full text), 'type' ('creative'), 'word_count',\n"
            "'tone' (matched to request), and 'target_audience'."
        )

    def execute(self, task: AgentTask, context: dict[str, Any]) -> AgentResult:
        """Generate creative content for the given task.

        Constructs a style-aware creative prompt from the task's context
        fields (content, style) and uses the 'infer_json' callable from the
        execution context to produce structured output.

        Args:
            task: The agent task containing the creative writing request.
            context: Execution context; must contain an 'infer_json' callable
                with signature ``(prompt: str, fallback: Any) -> dict``.

        Returns:
            An AgentResult with the generated creative content as structured
            JSON in the output field.
        """
        content = task.context.get("content", task.description)
        style = task.context.get("style", "professional")

        prompt = (
            f"Create creative content:\n{content[:TRUNCATE_CONTENT_ANALYSIS]}\n\nStyle: {style}\n\n"
            "Respond as JSON:\n"
            '{"content": "...", "type": "creative", "word_count": 0, "tone": "' + style + '"}'
        )

        fallback: dict[str, Any] = {
            "content": "",
            "type": "creative",
            "_is_fallback": True,
        }
        infer_json = context.get("infer_json")
        if infer_json is not None:
            result = infer_json(prompt, fallback=fallback)
            is_fallback = isinstance(result, dict) and result.get("_is_fallback") is True
        else:
            self._logger.warning("No infer_json callable in context, using fallback")
            result = fallback
            is_fallback = True

        return AgentResult(
            success=not is_fallback,
            output=result,
            metadata={"mode": "creative_writing", "_is_fallback": is_fallback},
        )
