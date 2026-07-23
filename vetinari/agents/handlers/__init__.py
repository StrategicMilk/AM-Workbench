"""Handler protocol for decomposed agent modes.

Each handler implements a single agent mode as an independent class with
its own prompt, execution logic, and tests. Agent classes become thin
routers that dispatch to the appropriate handler.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import (
    Any,
    Protocol,
    runtime_checkable,
)

from vetinari.agents.contracts import AgentResult, AgentTask
from vetinari.exceptions import AgentError

logger = logging.getLogger(__name__)


__all__ = [
    "BaseHandler",
    "HandlerProtocol",
    "HandlerRouter",
]


@runtime_checkable
class HandlerProtocol(Protocol):
    """Protocol for agent mode handlers.

    Any class that implements these four members (mode_name, description,
    execute, get_system_prompt) satisfies this protocol and can be registered
    with a HandlerRouter for mode dispatch.
    """

    @property
    def mode_name(self) -> str: ...

    @property
    def description(self) -> str: ...

    def execute(self, task: AgentTask, context: dict[str, Any]) -> AgentResult:
        """Execute the handler's logic for the given task.

        Args:
            task: The agent task to execute.
            context: Additional context for execution, including inference
                callables and configuration.

        Returns:
            The result of the handler's execution.
        """
        ...

    def get_system_prompt(self) -> str:
        """Produce the full system prompt injected into the LLM context for this handler's mode.

        Returns:
            The system prompt string used to configure the LLM for this mode.
        """
        ...


class BaseHandler(ABC):
    """Abstract base class for handlers with common functionality.

    Provides property storage for mode_name and description, and a
    per-handler logger. Subclasses must implement execute() and
    get_system_prompt().

    Args:
        mode_name: The unique mode identifier this handler serves.
        description: A human-readable summary of the handler's purpose.
    """

    def __init__(self, mode_name: str, description: str) -> None:
        self.mode_name = mode_name
        self.description = description
        self._logger = logging.getLogger(__name__ + "." + mode_name)

    @abstractmethod
    def execute(self, task: AgentTask, context: dict[str, Any]) -> AgentResult:
        """Execute the handler's logic for the given task.

        Args:
            task: The agent task to execute.
            context: Additional context for execution.

        Returns:
            The result of the handler's execution.
        """

    @abstractmethod
    def get_system_prompt(self) -> str:
        """Produce the full system prompt injected into the LLM context for this handler's mode."""


class HandlerRouter:
    """Routes mode requests to appropriate handler instances.

    Acts as the dispatch layer between agent classes and their mode handlers.
    Handlers are registered by mode name and looked up on each route() call.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, HandlerProtocol] = {}

    def register(self, handler: HandlerProtocol) -> None:
        """Register a handler for its mode.

        Args:
            handler: A handler instance satisfying HandlerProtocol. Its
                mode_name property determines the key under which it is stored.

        Raises:
            ValueError: If a handler for this mode is already registered.
        """
        if handler.mode_name in self._handlers:
            raise AgentError(
                f"Handler for mode {handler.mode_name!r} is already registered",
            )
        self._handlers[handler.mode_name] = handler
        logger.info("Registered handler for mode %r", handler.mode_name)

    def get_handler(self, mode: str) -> HandlerProtocol | None:
        """Get the handler for a mode, or None if not registered.

        Args:
            mode: The mode name to look up.

        Returns:
            The registered handler, or None if no handler exists for this mode.
        """
        return self._handlers.get(mode)

    def available_modes(self) -> list[str]:
        """Return list of registered mode names.

        Returns:
            Sorted list of all mode names that have registered handlers.
        """
        return sorted(self._handlers.keys())

    def route(self, mode: str, task: AgentTask, context: dict[str, Any]) -> AgentResult | None:
        """Route a task to the appropriate handler.

        Args:
            mode: The mode name to dispatch to.
            task: The agent task to execute.
            context: Additional context passed through to the handler.

        Returns:
            The handler's AgentResult, or None if no handler is registered
            for the requested mode.
        """
        handler = self._handlers.get(mode)
        if handler is None:
            return None
        return handler.execute(task, context)
