"""A2A HTTP/JSON-RPC transport layer.

Handles the wire-level framing for the Agent-to-Agent protocol.
Incoming requests arrive as JSON-RPC 2.0 dicts; this module routes them
to the correct handler, executes via the :class:`~vetinari.a2a.executor.VetinariA2AExecutor`,
and returns a well-formed JSON-RPC response dict.

No HTTP framework is used — the transport operates purely on Python dicts
so it can be embedded in any WSGI/ASGI handler (Flask route, test harness,
etc.) without coupling to a specific web stack.

Supported JSON-RPC methods:
    - ``a2a.getAgentCard``  — advertise the available agent cards
    - ``a2a.taskSend``      — accept and execute a new task
    - ``a2a.taskStatus``    — query the status of a previously submitted task
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from vetinari.a2a.agent_cards import get_all_cards
from vetinari.a2a.executor import A2ATask, VetinariA2AExecutor
from vetinari.utils.bounded_collections import BoundedDict

logger = logging.getLogger(__name__)


# ── JSON-RPC error codes ─────────────────────────────────────────────────────

_ERR_PARSE_ERROR = -32700  # Invalid JSON
_ERR_INVALID_REQUEST = -32600  # Not a valid JSON-RPC object
_ERR_METHOD_NOT_FOUND = -32601  # Method does not exist
_ERR_INVALID_PARAMS = -32602  # Invalid method parameters
_ERR_INTERNAL = -32603  # Internal server error

# A2A application-level error codes (outside the reserved -32768..-32000 range).
# These map to specific failure semantics so callers can distinguish them from
# generic internal errors.
_ERR_TASK_NOT_FOUND = -32001  # Requested task_id is unknown to this transport instance
_ERR_GUARDRAIL_BLOCKED = -32002  # Input was rejected by safety guardrails before execution
_MAX_IN_MEMORY_TASKS = 1000


# ── Internal sentinel exceptions ────────────────────────────────────────────
# Raised inside handlers and caught in handle_request so the right JSON-RPC
# error code is emitted.  Both are ValueError subclasses so they are caught by
# the (KeyError, TypeError, ValueError) branch only when NOT caught first.


class _TaskNotFoundError(ValueError):
    """Raised when a taskId lookup fails in _handle_task_status."""


class _GuardrailBlockedError(ValueError):
    """Raised when input guardrail rejects the task payload in _handle_task_send."""


_JSONRPC_ID_TYPES = (str, int)


# ── Transport ────────────────────────────────────────────────────────────────


class A2ATransport:
    """HTTP/JSON-RPC transport for the Vetinari A2A protocol endpoint.

    Decodes incoming JSON-RPC request dicts, dispatches to internal
    handlers, and returns JSON-RPC response dicts.  The transport is
    stateless apart from the executor it wraps; task status is stored in
    an in-memory dict so that callers can poll via ``a2a.taskStatus``.

    Attributes:
        host: Hostname this transport represents (used in card URLs).
        port: Port this transport represents (used in card URLs).

    Example::

        executor = VetinariA2AExecutor()
        transport = A2ATransport(host="localhost", port=8000, executor=executor)
        response = transport.handle_request({"jsonrpc": "2.0", "id": 1,
                                             "method": "a2a.getAgentCard",
                                             "params": {}})
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 8000,
        executor: VetinariA2AExecutor | None = None,
    ) -> None:
        """Initialise the transport with host, port, and executor.

        Args:
            host: Hostname to advertise in agent card URLs.
            port: Port to advertise in agent card URLs.
            executor: :class:`~vetinari.a2a.executor.VetinariA2AExecutor`
                instance to use for task execution.  A new instance is
                created if not provided.
        """
        self.host = host
        self.port = port
        self._executor = executor or VetinariA2AExecutor()
        # In-memory task store: task_id -> A2ATask (for status queries)
        self._tasks: BoundedDict[str, A2ATask] = BoundedDict(_MAX_IN_MEMORY_TASKS)
        logger.info("A2ATransport initialised on %s:%d", self.host, self.port)

    # ── Public API ────────────────────────────────────────────────────────────

    def handle_request(self, request_data: dict) -> dict:
        """Process a single JSON-RPC request and return a JSON-RPC response.

        Args:
            request_data: A JSON-RPC 2.0 request as a Python dict.  Must
                contain ``jsonrpc``, ``method``, and optionally ``id`` and
                ``params`` keys.

        Returns:
            JSON-RPC 2.0 response dict.  On success the ``result`` key
            contains the method-specific payload.  On error the ``error``
            key contains ``code``, ``message``, and optional ``data``.
        """
        if not isinstance(request_data, dict):
            return self._error_response(None, _ERR_INVALID_REQUEST, "Invalid Request: expected JSON object")

        raw_id = request_data.get("id")
        req_id = raw_id if self._is_supported_jsonrpc_id(raw_id) else None
        if "id" not in request_data or not self._is_supported_jsonrpc_id(raw_id):
            return self._error_response(req_id, _ERR_INVALID_REQUEST, "id must be a string or integer")

        method = request_data.get("method")
        params = request_data.get("params", {})

        logger.debug("A2A request method=%s id=%s", method, req_id)

        if request_data.get("jsonrpc") != "2.0":
            return self._error_response(req_id, _ERR_INVALID_REQUEST, "jsonrpc field must be '2.0'")

        if not isinstance(method, str) or not method:
            return self._error_response(req_id, _ERR_INVALID_REQUEST, "method field is required")

        if params is None:
            params = {}
        if not isinstance(params, dict):
            return self._error_response(req_id, _ERR_INVALID_PARAMS, "params must be an object")

        dispatch = {
            "a2a.getAgentCard": self._handle_agent_card_request,
            "a2a.taskSend": lambda p: self._handle_task_send(p),
            "a2a.taskStatus": lambda p: self._handle_task_status(p),
        }

        handler = dispatch.get(method)
        if handler is None:
            return self._error_response(
                req_id,
                _ERR_METHOD_NOT_FOUND,
                f"Method '{method}' not found. Supported: {sorted(dispatch.keys())}",
            )

        try:
            result = handler(params)
            return self._success_response(req_id, result)
        except _TaskNotFoundError as exc:
            # Task lookup failed — return a specific application error so callers
            # can distinguish "task not found" from generic internal failures.
            logger.warning("Task not found for method '%s': %s", method, exc)
            return self._error_response(req_id, _ERR_TASK_NOT_FOUND, str(exc))
        except _GuardrailBlockedError as exc:
            # Input was rejected by safety guardrails before any execution occurred.
            logger.warning("Guardrail blocked input for method '%s': %s", method, exc)
            return self._error_response(req_id, _ERR_GUARDRAIL_BLOCKED, str(exc))
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("Invalid params for method '%s': %s", method, exc)
            return self._error_response(req_id, _ERR_INVALID_PARAMS, str(exc))
        except Exception as exc:
            logger.exception("Internal error handling method '%s': %s", method, exc)
            return self._error_response(req_id, _ERR_INTERNAL, f"Internal error: {exc}")

    def handle_request_json(self, raw_json: str) -> str:
        """Parse a raw JSON string, process it, and return a JSON string.

        Convenience wrapper around :meth:`handle_request` for callers that
        operate on raw JSON bytes/strings.

        Args:
            raw_json: JSON-encoded string of the request.

        Returns:
            JSON-encoded string of the response.
        """
        try:
            request_data = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            logger.warning("JSON parse error in A2A request — request rejected: %s", exc)
            response = self._error_response(None, _ERR_PARSE_ERROR, f"Parse error: {exc}")
            return json.dumps(response)

        if not isinstance(request_data, dict):
            logger.warning(
                "A2A request body parsed as %s, expected JSON object — request rejected",
                type(request_data).__name__,
            )
            response = self._error_response(None, _ERR_INVALID_REQUEST, "Invalid Request: expected JSON object")
            return json.dumps(response)

        response = self.handle_request(request_data)
        return json.dumps(response)

    # ── Method handlers ───────────────────────────────────────────────────────

    @staticmethod
    def _handle_agent_card_request(params: dict) -> dict:
        """Return the agent cards for all pipeline agents.

        Args:
            params: JSON-RPC params (unused for this method).

        Returns:
            Dict with an ``agents`` list of serialised agent cards.
        """
        cards = get_all_cards()
        serialised = [card.to_dict() for card in cards]
        logger.debug("Returning %d agent cards", len(serialised))
        return {"agents": serialised}

    def _parse_task_send_params(self, params: dict[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any], str]:
        """Validate task-send params and allocate the task id."""
        task_type = params.get("taskType")
        if not isinstance(task_type, str) or not task_type:
            raise ValueError("taskType must be a non-empty string")

        supported_task_types = getattr(self._executor, "supported_task_types", None)
        if isinstance(supported_task_types, (list, tuple, set, frozenset)) and task_type not in supported_task_types:
            raise ValueError(f"Unsupported taskType: {task_type!r}")

        input_data = params.get("inputData", {})
        if input_data is None:
            input_data = {}
        if not isinstance(input_data, dict):
            raise ValueError("inputData must be an object")

        metadata = params.get("metadata", {})
        if metadata is None:
            metadata = {}
        if not isinstance(metadata, dict):
            raise ValueError("metadata must be an object")

        raw_task_id = params.get("taskId")
        if raw_task_id is None:
            task_id = str(uuid.uuid4())
        elif isinstance(raw_task_id, str) and raw_task_id:
            task_id = raw_task_id
        else:
            raise ValueError("taskId must be a non-empty string when provided")

        if self._task_id_exists(task_id):
            raise ValueError(f"Duplicate taskId: {task_id!r}")
        return task_type, input_data, metadata, task_id

    @staticmethod
    def _check_input_guardrails(task_id: str, input_text: str) -> None:
        """Reject an A2A task when input guardrails block or cannot run."""
        try:
            from vetinari.safety.guardrails import get_guardrails

            input_gr = get_guardrails().check_input(input_text)
            if not input_gr.allowed:
                logger.warning(
                    "A2A task id=%s input blocked by guardrails before execution — task rejected",
                    task_id,
                )
                raise _GuardrailBlockedError(f"Task '{task_id}' input was rejected by safety guardrails")
        except _GuardrailBlockedError:
            raise
        except Exception as exc:
            logger.exception(
                "Guardrail infrastructure failure for A2A task id=%s — rejecting task to fail closed",
                task_id,
            )
            raise _GuardrailBlockedError(f"Task '{task_id}' rejected: guardrail check could not complete") from exc

    @staticmethod
    def _redact_blocked_output(task_id: str, input_text: str, result_dict: dict[str, Any]) -> dict[str, Any]:
        """Redact output when post-execution guardrails block or fail."""
        output_text = str(result_dict.get("outputData") or result_dict.get("output") or "")
        try:
            from vetinari.safety.guardrails import get_guardrails

            _input_recheck, output_gr = get_guardrails().check_both(input_text, output_text)
            if not output_gr.allowed:
                logger.warning("A2A task id=%s output blocked by guardrails — redacting", task_id)
                if "outputData" in result_dict:
                    result_dict["outputData"] = "[Content filtered for safety]"
        except Exception:
            logger.warning(
                "Output guardrails check failed for A2A task id=%s — redacting output as fail-closed safety measure",
                task_id,
                exc_info=True,
            )
            if "outputData" in result_dict:
                result_dict["outputData"] = "[Content filtered for safety]"
        return result_dict

    def _handle_task_send(self, params: dict) -> dict:
        """Accept, store, and execute an incoming A2A task.

        Input guardrails run BEFORE execution so that blocked content is
        never passed to the pipeline.  Output guardrails run AFTER execution
        to filter the result; a blocked output is redacted rather than raised.

        If guardrail infrastructure itself raises, the task is rejected — both
        an explicit block decision and an infrastructure failure cause the task
        to be rejected (fail-closed).  A guardrail that cannot complete its
        check must never silently pass input through.

        Args:
            params: Must contain ``taskType`` and optionally ``inputData``
                and ``metadata`` keys.

        Returns:
            Dict with ``taskId``, ``status``, ``outputData``, and ``error``
            keys reflecting the execution result.

        Raises:
            KeyError: If ``taskType`` is missing from params.
            _GuardrailBlockedError: If the input payload is rejected by the
                safety guardrails before any execution occurs.
        """
        task_type, input_data, metadata, task_id = self._parse_task_send_params(params)

        # Build the plain-text representation used for guardrail checks.
        input_text = input_data.get("text") or input_data.get("prompt") or json.dumps(input_data)

        # ── Input guardrail: runs BEFORE execution (fail-closed on any failure) ──
        # Both an explicit "allowed=False" decision and any infrastructure error
        # (ImportError, unexpected exception) reject the task.  A guardrail that
        # cannot complete its check MUST NOT silently pass the input through —
        # that would be a default-pass verifier anti-pattern.
        self._check_input_guardrails(task_id, input_text)

        task = A2ATask(
            task_id=task_id,
            task_type=task_type,
            input_data=input_data,
            metadata=metadata,
        )
        self._tasks[task_id] = task

        logger.info("Received task id=%s type=%s", task_id, task_type)
        result = self._executor.execute(task)

        # ── Output guardrail: runs AFTER execution (redact, never raise) ────
        # Use check_both for defense-in-depth: re-verifies input alongside
        # output so any model-layer drift between the pre-execute check and
        # the post-execute check is caught at the trust boundary.  The input
        # re-verdict is informational (we already rejected blocked input
        # above); only the output verdict drives redaction here.
        result_dict = self._redact_blocked_output(task_id, input_text, result.to_dict())

        # Persist final task state
        self._tasks[task_id] = task
        return result_dict

    def _handle_task_status(self, params: dict) -> dict:
        """Return the current status of a previously submitted task.

        Args:
            params: Must contain a ``taskId`` key.

        Returns:
            Dict with ``taskId`` and ``status`` keys.

        Raises:
            KeyError: If ``taskId`` is missing from params.
            _TaskNotFoundError: If ``taskId`` is not recognised by this
                transport instance (triggers ``_ERR_TASK_NOT_FOUND`` in the
                JSON-RPC response rather than a generic internal error).
        """
        task_id = params.get("taskId")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("taskId must be a non-empty string")
        task = self._tasks.get(task_id)
        if task is None:
            persisted_status = self._lookup_persisted_task_status(task_id)
            if persisted_status is None:
                raise _TaskNotFoundError(
                    f"Task '{task_id}' not found. It may have never been submitted to this transport instance.",
                )
            return {"taskId": task_id, "status": persisted_status}
        logger.debug("Status query for task id=%s: %s", task_id, task.status)
        return {"taskId": task_id, "status": task.status}

    def _task_id_exists(self, task_id: str) -> bool:
        """Return True when a task ID already exists in memory or persistence."""
        if task_id in self._tasks:
            return True
        return self._lookup_persisted_task_status(task_id) is not None

    def _lookup_persisted_task_status(self, task_id: str) -> str | None:
        """Return a persisted A2A task status when durable state is available."""
        if not getattr(self._executor, "_db_available", False):
            return None
        try:
            from vetinari.database import get_connection

            row = (
                get_connection()
                .execute(
                    "SELECT status FROM a2a_tasks WHERE task_id = ?",
                    (task_id,),
                )
                .fetchone()
            )
        except Exception:
            logger.warning("A2A persisted status lookup failed for task %s", task_id)
            return None
        if row is None:
            return None
        return str(row[0])

    # ── JSON-RPC helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _is_supported_jsonrpc_id(value: Any) -> bool:
        """Return True for JSON-RPC IDs this transport supports."""
        return isinstance(value, _JSONRPC_ID_TYPES) and not isinstance(value, bool)

    @staticmethod
    def _success_response(req_id: Any, result: Any) -> dict:
        """Build a JSON-RPC 2.0 success response.

        Args:
            req_id: The request ``id`` value (may be None for notifications).
            result: The result payload to include.

        Returns:
            JSON-RPC 2.0 success response dict.
        """
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    @staticmethod
    def _error_response(req_id: Any, code: int, message: str, data: Any = None) -> dict:
        """Build a JSON-RPC 2.0 error response.

        Args:
            req_id: The request ``id`` value (may be None for notifications).
            code: JSON-RPC error code (e.g. -32601 for method-not-found).
            message: Human-readable error description.
            data: Optional additional error data.

        Returns:
            JSON-RPC 2.0 error response dict.
        """
        error: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        return {"jsonrpc": "2.0", "id": req_id, "error": error}
