r"""Schema Validator — vetinari.drift.schema_validator  (Phase 7).

Validates dataclass instances and plain dicts against a set of JSON-Schema-
style rules without requiring any external ``jsonschema`` dependency.

Rules supported
---------------
    required_keys   — list of keys that must be present
    forbidden_keys  — list of keys that must NOT be present
    key_types       — dict mapping key → expected Python type name
    version_pattern — regex the ``version`` field must match
    non_empty_keys  — keys whose value must be non-None and non-empty-string

Usage
-----
    from vetinari.drift.schema_validator import get_schema_validator

    v = get_schema_validator()
    v.register_schema("Plan", {
        "required_keys":   ["plan_id", "goal", "status"],
        "key_types":       {"plan_id": "str", "risk_score": "float"},
        "version_pattern": "^v\\d+\\.\\d+\\.\\d+$",
        "non_empty_keys":  ["plan_id", "goal"],
    })

    errors = v.validate("Plan", plan_instance)
    assert errors == [], errors
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import asdict, is_dataclass
from typing import Any

from vetinari.types import StatusEnum

logger = logging.getLogger(__name__)


_TYPE_MAP: dict[str, type] = {
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "list": list,
    "dict": dict,
}


def _to_dict(obj: Any) -> dict:
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    if isinstance(obj, dict):
        return obj
    return {"__value__": obj}


class SchemaValidator:
    """Thread-safe schema validator.  Singleton — use.

    ``get_schema_validator()``.
    """

    _instance: SchemaValidator | None = None
    _class_lock = threading.Lock()

    def __new__(cls) -> SchemaValidator:
        if cls._instance is None:
            with cls._class_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._setup()
        return cls._instance

    def _setup(self) -> None:
        self._lock = threading.RLock()
        self._schemas: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Schema management
    # ------------------------------------------------------------------

    def register_schema(self, name: str, schema: dict[str, Any]) -> None:
        """Register (or replace) a named validation schema.

        Args:
            name: The name.
            schema: The schema.
        """
        with self._lock:
            self._schemas[name] = schema
        logger.debug("Registered schema '%s'", name)

    def unregister_schema(self, name: str) -> bool:
        """Unregister schema.

        Returns:
            True if successful, False otherwise.
        """
        with self._lock:
            existed = name in self._schemas
            self._schemas.pop(name, None)
            return existed

    def list_schemas(self) -> list[str]:
        """List schemas.

        Returns:
            Alphabetically sorted list of all registered schema names.
        """
        with self._lock:
            return sorted(self._schemas.keys())

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate(self, schema_name: str, obj: Any) -> list[str]:
        """Validate ``obj`` against the named schema.

        Returns a list of error strings (empty = valid).

        Args:
            schema_name: The schema name.
            obj: The obj.

        Returns:
            List of validation error strings describing each rule that was
            violated (required keys, type mismatches, empty fields, etc.).
            An empty list means the object is fully valid against the schema.
        """
        with self._lock:
            schema = self._schemas.get(schema_name)
        if schema is None:
            return [f"Unknown schema '{schema_name}'"]

        data = _to_dict(obj)

        # required_keys
        errors: list[str] = [
            f"Missing required key '{key}'" for key in schema.get("required_keys", []) if key not in data
        ]

        # forbidden_keys
        errors.extend(f"Forbidden key present: '{key}'" for key in schema.get("forbidden_keys", []) if key in data)

        # key_types
        for key, type_name in schema.get("key_types", {}).items():
            if key in data and data[key] is not None:
                expected = _TYPE_MAP.get(type_name)
                if expected and not isinstance(data[key], expected):
                    errors.append(f"Key '{key}': expected {type_name}, got {type(data[key]).__name__}")

        # version_pattern
        pattern = schema.get("version_pattern")
        if pattern and "version" in data:
            v = str(data["version"])
            if not re.match(pattern, v):
                errors.append(f"Field 'version' value '{v}' does not match pattern '{pattern}'")

        # non_empty_keys
        for key in schema.get("non_empty_keys", []):
            val = data.get(key)
            if val is None or val == "" or val == [] or val == {}:
                errors.append(f"Key '{key}' must not be empty")

        # allowed_status_values
        allowed_statuses = schema.get("allowed_status_values")
        if allowed_statuses and "status" in data and data["status"] not in allowed_statuses:
            errors.append(f"Invalid status '{data['status']}'; allowed: {allowed_statuses}")

        return errors

    def validate_many(
        self,
        schema_name: str,
        objects: list[Any],
    ) -> dict[int, list[str]]:
        """Validate a list of objects.  Returns index → error list (only failures).

        Args:
            schema_name: The schema name.
            objects: The objects.

        Returns:
            Mapping of list index to validation error strings for every object
            that failed validation.  Objects that pass are omitted, so an
            empty dict means all objects are valid.
        """
        failures: dict[int, list[str]] = {}
        for i, obj in enumerate(objects):
            errs = self.validate(schema_name, obj)
            if errs:
                failures[i] = errs
        return failures

    def is_valid(self, schema_name: str, obj: Any) -> bool:
        """Return True when validation produces no errors."""
        return len(self.validate(schema_name, obj)) == 0

    # ------------------------------------------------------------------
    # Built-in Vetinari schemas
    # ------------------------------------------------------------------

    def register_vetinari_schemas(self) -> None:
        """Register all canonical Vetinari contract schemas."""
        self.register_schema(
            "Plan",
            {
                "required_keys": ["plan_id", "goal", "status"],
                "non_empty_keys": ["plan_id", "goal"],
                "key_types": {"plan_id": "str", "goal": "str", "risk_score": "float"},
                "version_pattern": r"^v\d+\.\d+\.\d+$",
                "allowed_status_values": [
                    "draft",
                    "approved",
                    "executing",
                    StatusEnum.COMPLETED.value,
                    StatusEnum.FAILED.value,
                    "rejected",
                    StatusEnum.CANCELLED.value,
                ],
            },
        )
        self.register_schema(
            "Subtask",
            {
                "required_keys": ["subtask_id", "plan_id", "description", "status"],
                "non_empty_keys": ["subtask_id", "description"],
                "key_types": {"subtask_id": "str", "plan_id": "str", "depth": "int"},
            },
        )
        self.register_schema(
            "LogRecord",
            {
                "required_keys": ["message", "level", "timestamp"],
                "non_empty_keys": ["message", "level"],
                "key_types": {"message": "str", "level": "str", "timestamp": "float"},
            },
        )
        self.register_schema(
            "AlertThreshold",
            {
                "required_keys": ["name", "metric_key", "condition", "threshold_value", "severity"],
                "non_empty_keys": ["name", "metric_key"],
                "key_types": {"name": "str", "threshold_value": "float"},
            },
        )
        self.register_schema(
            "CostEntry",
            {
                "required_keys": ["provider", "model"],
                "non_empty_keys": ["provider", "model"],
                "key_types": {"cost_usd": "float", "input_tokens": "int", "output_tokens": "int"},
            },
        )
        self.register_schema(
            "ForecastResult",
            {
                "required_keys": ["metric", "method", "horizon", "predictions"],
                "non_empty_keys": ["metric", "method"],
                "key_types": {"horizon": "int", "rmse": "float"},
            },
        )
        logger.info("Registered %d Vetinari schemas", len(self._schemas))

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def get_stats(self) -> dict[str, Any]:
        """Get stats.

        Returns:
            Dictionary with ``registered_schemas`` (count) and
            ``schema_names`` (list of all registered schema names in
            registration order).
        """
        with self._lock:
            return {
                "registered_schemas": len(self._schemas),
                "schema_names": list(self._schemas.keys()),
            }

    def clear(self) -> None:
        """Clear for the current context."""
        with self._lock:
            self._schemas.clear()


# ---------------------------------------------------------------------------
# Singleton helpers
# ---------------------------------------------------------------------------


def get_schema_validator() -> SchemaValidator:
    """Return the module-level SchemaValidator singleton, creating it on first call.

    Returns:
        The shared SchemaValidator instance.
    """
    return SchemaValidator()


def reset_schema_validator() -> None:
    """Reset schema validator."""
    with SchemaValidator._class_lock:
        SchemaValidator._instance = None
