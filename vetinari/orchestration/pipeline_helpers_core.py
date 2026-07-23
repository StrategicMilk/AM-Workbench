"""Core service wiring and model-routing helpers for pipeline support."""

from __future__ import annotations

import contextlib
import importlib
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import vetinari.models.dynamic_model_router as dynamic_model_router
from vetinari.orchestration.execution_graph import ExecutionTaskNode

logger = logging.getLogger(__name__)


class PipelineCoreServicesMixin:
    """Variant, handler, agent, and model-routing support methods."""

    if TYPE_CHECKING:
        _AGENT_MODULE_MAP: Any
        _agents: Any
        _variant_manager: Any
        agent_context: Any
        execution_engine: Any
        model_router: Any

    def get_variant_config(self) -> Any:
        """Return the active VariantConfig controlling token and depth limits.

        Returns:
            The VariantConfig for the current processing depth level.
        """
        return self._variant_manager.get_config()

    def set_variant_level(self, level: str) -> Any:
        """Switch the processing depth level and return the new config.

        Args:
            level: One of ``"low"``, ``"medium"``, or ``"high"``.

        Returns:
            The VariantConfig for the newly selected level.

        Raises:
            ValueError: If *level* is not a recognised VariantLevel value.
        """
        config = self._variant_manager.set_level(level)
        logger.info(
            "Variant level changed to %s (max_context_tokens=%s, max_planning_depth=%s)",
            level,
            config.max_context_tokens,
            config.max_planning_depth,
        )
        return config

    def set_task_handlers(self, handlers: dict[str, Callable[..., Any]]) -> None:
        """Register task type handlers with the execution engine.

        Args:
            handlers: Mapping of task type string to handler callable.
        """
        for task_type, handler in handlers.items():
            self.execution_engine.register_handler(task_type, handler)

    def set_agent_context(self, context: dict[str, Any]) -> None:
        """Replace the shared agent context (adapter_manager, web_search, etc.).

        Clears the cached agent instances so they are re-initialized with the
        new context on their next use.

        Args:
            context: New agent context dict.
        """
        self.agent_context = context
        self._agents.clear()

    def _get_agent(self, agent_type_str: str) -> Any:
        """Get or create an agent by type string, initialized with shared context.

        Caches agent instances in ``self._agents`` keyed by uppercased type
        string. Uses dynamic import via ``_AGENT_MODULE_MAP`` to avoid circular
        imports at module load time.

        Args:
            agent_type_str: Agent type string (e.g. ``"FOREMAN"``).

        Returns:
            The agent instance, or None if the type is unknown or the import
            fails.
        """
        key = agent_type_str.upper()
        if key in self._agents:
            return self._agents[key]
        if key not in self._AGENT_MODULE_MAP:
            logger.debug("No agent module registered for type: %s", key)
            return None
        try:
            mod_path, fn_name = self._AGENT_MODULE_MAP[key]
            mod = importlib.import_module(mod_path)
            getter = getattr(mod, fn_name, None)
            if getter is None:
                return None
            agent = getter()
            if self.agent_context:
                agent.initialize(self.agent_context)
            self._agents[key] = agent
            return agent
        except Exception as e:
            logger.warning("Could not get agent '%s': %s", key, e)
            return None

    def _route_model_for_task(self, task: ExecutionTaskNode) -> str:
        """Select the best model for a task using dynamic model routing.

        Falls back to ``"auto"`` (resolved by the adapter to the best
        available model) when the model router is unavailable or raises.

        Args:
            task: The task node to route.

        Returns:
            Model ID string, or ``"auto"`` as fallback.
        """
        if self.model_router is None:
            try:
                self.model_router = dynamic_model_router.get_model_router()
            except Exception:
                logger.warning(
                    "Model router unavailable for task %s - falling back to 'auto'",
                    task.id,
                )
                return "auto"  # Adapter resolves "auto" to best available model
        try:

            def _task_type(names: tuple[str, ...], value: str) -> Any:
                for name in names:
                    candidate = getattr(dynamic_model_router.TaskType, name, None)
                    if candidate is not None:
                        return candidate
                with contextlib.suppress(Exception):
                    return dynamic_model_router.TaskType(value)
                return getattr(dynamic_model_router.TaskType, "GENERAL", "general")

            task_type_map = {
                "analysis": _task_type(("RESEARCH", "ANALYSIS"), "research"),
                "implementation": _task_type(("CODE", "CODING"), "code"),
                "testing": _task_type(("TESTING",), "testing"),
                "research": _task_type(("RESEARCH", "ANALYSIS"), "research"),
                "documentation": _task_type(("DOCS", "DOCUMENTATION"), "docs"),
                "verification": _task_type(("CODE_REVIEW",), "code_review"),
                # Phase 7 additions
                "creative_writing": _task_type(("CREATIVE", "CREATIVE_WRITING"), "creative"),
                "security_audit": _task_type(("SECURITY", "SECURITY_AUDIT"), "security"),
                "devops": _task_type(("DEVOPS",), "devops"),
                "image_generation": _task_type(("IMAGE", "IMAGE_GENERATION"), "image"),
                "cost_analysis": _task_type(("COST_ANALYSIS",), "cost_analysis"),
                "specification": _task_type(("SPECIFICATION",), "specification"),
                "creative": _task_type(("CREATIVE",), "creative"),
                "security": _task_type(("SECURITY", "SECURITY_AUDIT"), "security"),
            }
            t_type = task_type_map.get(task.task_type.lower(), _task_type(("GENERAL",), "general"))
            selection = self.model_router.select_model(t_type)
            if selection and selection.model:
                # Store confidence on the task node so pipeline stages can check it
                if selection.confidence_result is not None:
                    task.input_data["_selection_confidence"] = selection.confidence_result.score
                    task.input_data["_selection_confidence_level"] = selection.confidence_result.level.value
                    task.input_data["_selection_confidence_explanation"] = selection.confidence_result.explanation
                # Store "I don't know" protocol messages for downstream visibility
                if selection.unknown_situations:
                    task.input_data["_unknown_situations"] = [
                        {"situation": p.situation.value, "message": p.message, "action": p.action}
                        for p in selection.unknown_situations
                    ]
                return selection.model.id
        except Exception as e:
            logger.warning("Model routing failed for task %s: %s", task.id, e)
        return "auto"  # Adapter resolves "auto" to best available model
