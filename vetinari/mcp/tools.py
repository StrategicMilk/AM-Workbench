"""MCP Tool definitions for Vetinari."""

from __future__ import annotations

import inspect
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast

from vetinari.security.fail_closed import sanitize_untrusted_text
from vetinari.security.redaction import redact_text
from vetinari.utils import privacy_receipt
from vetinari.utils.registry import BaseRegistry

logger = logging.getLogger(__name__)

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_JSON_SCHEMA_TYPES = frozenset({"string", "number", "integer", "boolean", "object", "array"})


@dataclass(frozen=True, slots=True)
class MCPToolParameter:
    """A single parameter definition for an MCP tool.

    Attributes:
        name: Parameter name used as the keyword argument key.
        type: JSON Schema type string (e.g. "string", "number", "boolean", "object").
        description: Human-readable description shown in the tool schema.
        required: Whether the parameter must be supplied by the caller.
        default: Default value used when ``required`` is False and the caller omits the parameter.
    """

    name: str
    type: str  # "string", "number", "boolean", "object"
    description: str
    required: bool = True
    default: Any = None

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return f"MCPToolParameter(name={self.name!r}, type={self.type!r}, required={self.required!r})"


@dataclass
class MCPTool:
    """Model Context Protocol tool definition.

    Bundles the schema metadata (name, description, parameters) with the
    Python callable that implements the tool's behaviour.
    """

    name: str
    description: str
    parameters: list[MCPToolParameter] = field(default_factory=list)
    handler: Callable | None = None

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return f"MCPTool(name={self.name!r}, parameters={len(self.parameters)!r})"

    def to_schema(self) -> dict[str, Any]:
        """Convert to MCP tool schema format.

        Returns:
            A dict conforming to the MCP inputSchema specification, with
            ``name``, ``description``, and ``inputSchema`` keys.
        """
        _validate_tool_metadata(self)
        _validate_tool_signature(self)
        properties = {}
        required = []
        for p in self.parameters:
            properties[p.name] = {"type": p.type, "description": p.description}
            if p.required:
                required.append(p.name)
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        }


def _validate_tool_signature(tool: MCPTool) -> None:
    """Ensure advertised MCP parameters match an inspectable keyword handler."""
    if tool.handler is None:
        return
    try:
        signature = inspect.signature(tool.handler)
    except (TypeError, ValueError):
        logger.warning("MCP tool %s handler signature is not inspectable; schema drift check skipped", tool.name)
        return

    parameters = list(signature.parameters.values())
    if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters):
        return

    keyword_parameters = [
        parameter
        for parameter in parameters
        if parameter.kind in {inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY}
    ]
    handler_names = {parameter.name for parameter in keyword_parameters}
    schema_names = {parameter.name for parameter in tool.parameters}
    missing_from_schema = sorted(handler_names - schema_names)
    missing_from_handler = sorted(schema_names - handler_names)
    if missing_from_schema or missing_from_handler:
        parts = []
        if missing_from_schema:
            parts.append(f"handler-only={missing_from_schema}")
        if missing_from_handler:
            parts.append(f"schema-only={missing_from_handler}")
        raise ValueError(
            f"MCP tool {tool.name!r} parameter schema does not match handler signature: {'; '.join(parts)}"
        )

    declared = {parameter.name: parameter for parameter in tool.parameters}
    required_mismatches = []
    for parameter in keyword_parameters:
        handler_required = parameter.default is inspect.Parameter.empty
        if declared[parameter.name].required is not handler_required:
            required_mismatches.append(parameter.name)
    if required_mismatches:
        raise ValueError(
            f"MCP tool {tool.name!r} required parameters do not match handler defaults: "
            f"{', '.join(sorted(required_mismatches))}"
        )


def _validate_identifier(value: object, *, label: str) -> str:
    text = sanitize_untrusted_text(value, max_length=128)
    if not _IDENTIFIER_RE.fullmatch(text):
        from vetinari.security.fail_closed import UntrustedInputError

        raise UntrustedInputError(f"{label} contains unsupported characters")
    return text


def _validate_tool_metadata(tool: MCPTool) -> None:
    """Fail closed before tool schemas are exposed to worker prompts."""
    _validate_identifier(tool.name, label="tool name")
    sanitize_untrusted_text(tool.description, max_length=2_000)
    seen: set[str] = set()
    for parameter in tool.parameters:
        name = _validate_identifier(parameter.name, label="parameter name")
        if name in seen:
            from vetinari.security.fail_closed import UntrustedInputError

            raise UntrustedInputError(f"duplicate parameter name: {name}")
        seen.add(name)
        if parameter.type not in _JSON_SCHEMA_TYPES:
            from vetinari.security.fail_closed import UntrustedInputError

            raise UntrustedInputError(f"unsupported parameter type: {parameter.type!r}")
        sanitize_untrusted_text(parameter.description, max_length=1_000)


class MCPToolRegistry(BaseRegistry[str, MCPTool]):
    """Registry of MCP tools available to external callers."""

    def register(self, tool: MCPTool | str, item: MCPTool | None = None) -> None:
        """Register a tool, keying it on its name by default.

        Args:
            tool: The MCPTool instance to register, or an explicit registry key.
            item: Optional explicit MCPTool item for BaseRegistry-compatible calls.

        Raises:
            TypeError: If called with an invalid key/item shape.
        """
        if item is None:
            if not isinstance(tool, MCPTool):
                msg = "register(tool) requires an MCPTool when item is omitted"
                raise TypeError(msg)
            _validate_tool_metadata(tool)
            _validate_tool_signature(tool)
            super().register(tool.name, tool)
            return

        if not isinstance(tool, str):
            msg = "register(key, item) requires a string key"
            raise TypeError(msg)
        _validate_identifier(tool, label="registry key")
        _validate_tool_metadata(item)
        _validate_tool_signature(item)
        super().register(tool, item)

    def list_tools(self) -> list[dict[str, Any]]:
        """Return the MCP schema for every registered tool.

        Returns:
            A list of tool schema dicts suitable for returning in an MCP
            ``tools/list`` response.
        """
        return [t.to_schema() for t in self.list_all()]

    def invoke(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Invoke a registered tool by name and return its result.

        Args:
            name: The name of the tool to invoke.
            arguments: Keyword arguments forwarded to the tool's handler.

        Returns:
            On success: ``{"result": <handler return value>}``.
            On failure: ``{"error": <error message>}``.
        """
        tool = self.get(name)
        if tool is None:
            return {"error": f"Unknown tool: {name}"}
        if tool.handler is None:
            return {"error": f"Tool '{name}' has no handler"}
        validation_error = self._validate_arguments(tool, arguments)
        if validation_error is not None:
            return {"error": validation_error, "category": "client_error"}
        try:
            result = tool.handler(**arguments)
            return {"result": result}
        except (ValueError, TypeError, KeyError) as exc:
            logger.warning("Tool %s received invalid input: %s", name, exc)
            return {"error": f"Tool '{name}' received invalid input", "category": "client_error"}
        except Exception as exc:
            logger.exception("Tool %s execution failed", name)
            return {"error": f"Tool '{name}' execution failed: {type(exc).__name__}", "category": "server_error"}

    def _validate_arguments(self, tool: MCPTool, arguments: dict[str, Any]) -> str | None:
        """Validate tool arguments against the advertised MCP input schema."""
        if not isinstance(arguments, dict):
            return "arguments must be an object"

        params = {parameter.name: parameter for parameter in tool.parameters}
        for parameter in tool.parameters:
            if parameter.required and parameter.name not in arguments:
                return f"missing required argument: {parameter.name}"

        extra = sorted(set(arguments) - set(params))
        if extra:
            return f"unsupported argument(s): {', '.join(extra)}"

        for key, value in arguments.items():
            parameter = params[key]
            if not self._value_matches_type(value, parameter.type):
                return f"argument {key!r} must be {parameter.type}"
        return None

    @staticmethod
    def _value_matches_type(value: Any, schema_type: str) -> bool:
        """Return True when *value* matches a simple JSON Schema type."""
        if schema_type == "string":
            return isinstance(value, str)
        if schema_type in {"number", "integer"}:
            if isinstance(value, bool):
                return False
            if schema_type == "integer":
                return isinstance(value, int)
            return isinstance(value, (int, float))
        if schema_type == "boolean":
            return isinstance(value, bool)
        if schema_type == "object":
            return isinstance(value, dict)
        if schema_type == "array":
            return isinstance(value, list)
        return True

    def register_external_tool(self, server_name: str, tool: MCPTool) -> None:
        """Register an external MCP tool under the ``mcp__{server}__{tool}`` namespace.

        The namespacing convention ``mcp__{server_name}__{tool_name}`` prevents
        name collisions between Vetinari's built-in tools and tools from external
        MCP servers.  The ``tool.name`` field is rewritten on a copy so the
        original MCPTool object is not mutated.

        Args:
            server_name: Logical name of the external MCP server (e.g. ``"filesystem"``).
            tool: The MCPTool to register; its ``name`` will be prefixed.
        """
        import dataclasses

        namespaced_name = f"mcp__{server_name}__{tool.name}"
        namespaced_tool = dataclasses.replace(tool, name=namespaced_name)
        self.register(namespaced_tool)
        logger.debug("Registered external MCP tool %r from server %r", namespaced_name, server_name)

    def unregister_external_server(self, server_name: str) -> int:
        """Remove all tools registered for an external MCP server.

        Returns:
            Number of tools removed from the registry.
        """
        prefix = f"mcp__{server_name}__"
        removed = 0
        for name in list(self.list_keys()):
            if isinstance(name, str) and name.startswith(prefix):
                self.unregister(name)
                removed += 1
        return removed

    def register_external_server(self, server_name: str, client: Any) -> list[str]:
        """Discover tools from a running MCPClient and register them with namespace prefix.

        Calls ``client.list_tools()`` to get tool schemas, converts each schema
        to an ``MCPTool`` with a handler that delegates back to the client, then
        registers each tool via ``register_external_tool()``.

        Args:
            server_name: Logical name for this external server (used as namespace prefix).
            client: A started ``MCPClient`` instance whose ``list_tools()`` and
                ``call_tool()`` methods will be used.

        Returns:
            List of namespaced tool names that were successfully registered.

        Raises:
            Exception: Any error from ``client.list_tools()`` propagates — callers
                should wrap in try/except when discovery failures are non-fatal.
        """
        raw_tools: list[dict[str, Any]] = client.list_tools()
        registered: list[str] = []

        for schema in raw_tools:
            tool_name: str = schema.get("name", "")
            if not tool_name:
                logger.warning("Skipping external tool from %r with missing name", server_name)
                continue

            description: str = schema.get("description", f"External tool from {server_name}")
            input_schema: dict[str, Any] = schema.get("inputSchema", {})
            properties: dict[str, Any] = input_schema.get("properties", {})
            required_params: list[str] = input_schema.get("required", [])

            parameters = [
                MCPToolParameter(
                    name=param_name,
                    type=param_def.get("type", "string"),
                    description=param_def.get("description", param_name),
                    required=param_name in required_params,
                )
                for param_name, param_def in properties.items()
            ]

            # Capture current values for the closure — avoids late-binding bugs
            # in the per-tool handler lambda inside the loop.
            captured_name = tool_name
            captured_client = client

            def _make_handler(t_name: str, t_client: Any) -> Any:
                def _handler(**kwargs: Any) -> dict[str, Any]:
                    text = t_client.call_tool(t_name, kwargs)
                    return {"text": text}

                return _handler

            tool = MCPTool(
                name=tool_name,
                description=description,
                parameters=parameters,
                handler=_make_handler(captured_name, captured_client),
            )
            self.register_external_tool(server_name, tool)
            registered.append(f"mcp__{server_name}__{tool_name}")

        logger.info(
            "Registered %d external tool(s) from MCP server %r",
            len(registered),
            server_name,
        )
        return registered

    def register_defaults(self) -> None:
        """Register the five default Vetinari MCP tools.

        Tools registered:
        - ``vetinari_plan``: Generate an execution plan from a goal description.
        - ``vetinari_search``: Semantic codebase search via CocoIndexAdapter.
        - ``vetinari_execute``: Execute a task through the Vetinari pipeline.
        - ``vetinari_memory``: Query or store entries in the dual memory system.
        - ``vetinari_benchmark``: Run a named benchmark suite and return a summary.
        """
        self.register(
            MCPTool(
                name="vetinari_plan",
                description="Generate an execution plan from a goal description",
                parameters=[
                    MCPToolParameter("goal", "string", "The goal to plan for"),
                    MCPToolParameter("context", "string", "Additional context", required=False),
                ],
                handler=self._handle_plan,
            ),
        )
        self.register(
            MCPTool(
                name="vetinari_search",
                description="Search codebase with semantic understanding",
                parameters=[
                    MCPToolParameter("query", "string", "Search query"),
                    MCPToolParameter("limit", "number", "Max results", required=False, default=10),
                ],
                handler=self._handle_search,
            ),
        )
        self.register(
            MCPTool(
                name="vetinari_execute",
                description="Execute a task through the Vetinari pipeline",
                parameters=[
                    MCPToolParameter("task", "string", "Task description"),
                ],
                handler=self._handle_execute,
            ),
        )
        self.register(
            MCPTool(
                name="vetinari_memory",
                description="Query or store in Vetinari memory system",
                parameters=[
                    MCPToolParameter("action", "string", "Action: query, store, recall"),
                    MCPToolParameter("content", "string", "Content to query/store"),
                ],
                handler=self._handle_memory,
            ),
        )
        self.register(
            MCPTool(
                name="vetinari_benchmark",
                description="Run a benchmark suite",
                parameters=[
                    MCPToolParameter("suite", "string", "Benchmark suite name"),
                    MCPToolParameter("limit", "number", "Max cases", required=False, default=10),
                ],
                handler=self._handle_benchmark,
            ),
        )

    # ── Tool handlers ──────────────────────────────────────────────────────────

    @staticmethod
    def _handle_plan(goal: str, context: str = "") -> dict[str, Any]:
        """Generate an execution plan for *goal* via TwoLayerOrchestrator.

        Delegates to ``TwoLayerOrchestrator.generate_plan_only()`` which runs
        the PlanGenerator without triggering any actual task execution.

        Args:
            goal: Natural-language description of the goal to plan for.
            context: Optional additional context passed as a constraint.

        Returns:
            A dict with keys ``plan_id``, ``goal``, and ``steps`` (list of
            ``{"id", "description", "dependencies"}`` dicts).  On failure,
            returns ``{"error": <message>, "tool": "vetinari_plan"}``.
        """
        try:
            from vetinari.orchestration.two_layer import get_two_layer_orchestrator

            orch = get_two_layer_orchestrator()
            constraints = {"context": context} if context else None
            graph = orch.generate_plan_only(goal, constraints=constraints)
            steps = [
                {
                    "id": nid,
                    "description": node.description,
                    "dependencies": list(node.depends_on),
                }
                for nid, node in graph.nodes.items()
            ]
            return {"plan_id": graph.plan_id, "goal": goal, "steps": steps}
        except Exception as exc:
            logger.exception("vetinari_plan failed for goal %r", goal)
            return {"error": f"Plan generation failed: {type(exc).__name__}", "tool": "vetinari_plan"}

    @staticmethod
    def _handle_search(query: str, limit: int = 10) -> dict[str, Any]:
        """Search the codebase using CocoIndexAdapter semantic search.

        Args:
            query: Natural-language or keyword search query.
            limit: Maximum number of results to return (default 10).

        Returns:
            A dict with a ``results`` list.  On failure, returns a dict with
            ``results`` set to an empty list, a ``note`` explaining the issue,
            and ``tool`` set to ``"vetinari_search"``.
        """
        try:
            from vetinari.code_search import CocoIndexAdapter

            searcher = CocoIndexAdapter()
            results = searcher.search(query, limit=limit)
            return {"results": results}
        except Exception:
            logger.exception("vetinari_search failed for query %r", query)
            return {
                "results": [],
                "error": "Search subsystem not available",
                "tool": "vetinari_search",
            }

    @staticmethod
    def _handle_execute(task: str) -> dict[str, Any]:
        """Execute a task through the Vetinari pipeline via TwoLayerOrchestrator.

        Calls ``execute_with_agent_graph()`` which generates a plan and then
        delegates execution to AgentGraph, falling back to
        ``generate_and_execute()`` when AgentGraph is unavailable.

        Args:
            task: Natural-language description of the task to execute.

        Returns:
            A dict with keys ``plan_id``, ``goal``, ``backend``, ``completed``,
            ``failed``, ``outputs``, and ``errors`` as returned by the
            orchestrator.  On failure, returns
            ``{"error": <message>, "tool": "vetinari_execute"}``.
        """
        try:
            from vetinari.orchestration.two_layer import get_two_layer_orchestrator

            orch = get_two_layer_orchestrator()
            return cast(dict[str, Any], orch.execute_with_agent_graph(task))
        except Exception as exc:
            logger.exception("vetinari_execute failed for task %r", task)
            return {"error": f"Task execution failed: {type(exc).__name__}", "tool": "vetinari_execute"}

    @staticmethod
    def _handle_memory(action: str, content: str) -> dict[str, Any]:
        """Query or store an entry in the Vetinari dual memory system.

        Supports three actions:
        - ``"store"``: Persist *content* as a new memory entry.
        - ``"recall"``: Full-text search for entries matching *content*.
        - ``"query"``: Alias for ``"recall"``.

        Args:
            action: One of ``"store"``, ``"recall"``, or ``"query"``.
            content: Text to store or use as the search query.

        Returns:
            For ``"store"``: ``{"status": "stored", "id": <entry_id>}``.
            For ``"recall"`` / ``"query"``: ``{"entries": [<content strings>]}``.
            For unknown action: ``{"error": "Unknown action: <action>", "tool": "vetinari_memory"}``.
            On subsystem failure: ``{"error": <message>, "tool": "vetinari_memory"}``.
        """
        try:
            from vetinari.memory import get_unified_memory_store
            from vetinari.memory.interfaces import MemoryEntry

            store = get_unified_memory_store()

            if action == "store":
                safe_content = redact_text(content)
                entry = MemoryEntry(
                    content=safe_content,
                    agent="mcp",
                    metadata={
                        "privacy_receipt": privacy_receipt(
                            privacy_class="subject_data",
                            subject_id="mcp-local-user",
                            retention_days=30,
                            source="mcp.memory.store",
                            redaction_applied=safe_content != content,
                        )
                    },
                )
                entry_id = store.remember(entry)
                return {"status": "stored", "id": entry_id}

            if action in ("recall", "query"):
                results = store.search(content, limit=5)
                return {"entries": [r.content for r in results]}

            return {"error": f"Unknown action: {action}", "tool": "vetinari_memory"}

        except Exception as exc:
            logger.exception("vetinari_memory failed for action %r", action)
            return {"error": f"Memory operation failed: {type(exc).__name__}", "tool": "vetinari_memory"}

    @staticmethod
    def _handle_benchmark(suite: str, limit: int = 10) -> dict[str, Any]:
        """Run a named benchmark suite and return its summary.

        Args:
            suite: Name of the benchmark suite to run.
            limit: Maximum number of test cases to execute (default 10).

        Returns:
            The benchmark runner's summary dict on success.  On failure,
            returns ``{"error": <message>, "tool": "vetinari_benchmark"}``.
        """
        try:
            from vetinari.benchmarks import get_default_runner

            runner = get_default_runner()
            report = runner.run_suite(suite, limit=limit)
            return cast(dict[str, Any], report.summary_dict())
        except Exception as exc:
            logger.exception("vetinari_benchmark failed for suite %r", suite)
            return {"error": f"Benchmark execution failed: {type(exc).__name__}", "tool": "vetinari_benchmark"}
