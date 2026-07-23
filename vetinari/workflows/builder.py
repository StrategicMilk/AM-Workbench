"""Pipeline-definition backend for the Workflow Builder surface.

Stores pipeline graphs as YAML files under ``outputs/workflows/``.  Each
pipeline is a directed graph of typed nodes connected by conditional edges.
The module performs validation (node-reference integrity, cycle detection) and
dispatches execution hooks without directly executing tasks.

Public API
----------
create_pipeline  — construct a new Pipeline value object
save_pipeline    — write a Pipeline to ``outputs/workflows/<id>.yaml``
load_pipeline    — read a Pipeline from disk by pipeline_id
list_pipelines   — enumerate all pipeline IDs on disk
validate_pipeline — return a list of error strings (empty = valid)
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from vetinari.constants import OUTPUTS_DIR
from vetinari.security.fail_closed import (
    PathTraversalError,
    SchemaOpenError,
    UntrustedInputError,
    assert_closed_schema,
    confine_to_root,
    sanitize_untrusted_text,
)

logger = logging.getLogger(__name__)

# -- Storage root --
# All pipeline YAML files are stored here.  The directory is created lazily so
# that import-time I/O is avoided (module-level I/O anti-pattern).
_WORKFLOWS_DIR_NAME = "workflows"
_PIPELINE_ID_ALLOWED = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
_PIPELINE_KEYS = {"pipeline_id", "name", "created_at", "updated_at", "nodes", "edges"}
_NODE_KEYS = {"node_id", "node_type", "params"}
_EDGE_KEYS = {"from_node", "to_node", "condition"}


def _safe_text(value: object, *, label: str, max_length: int = 4_000) -> str:
    try:
        return sanitize_untrusted_text(str(value), max_length=max_length)
    except UntrustedInputError as exc:
        raise UntrustedInputError(f"{label} is not safe workflow input") from exc


def _safe_pipeline_id(value: object) -> str:
    pipeline_id = _safe_text(value, label="pipeline_id", max_length=160)
    if not pipeline_id or any(char not in _PIPELINE_ID_ALLOWED for char in pipeline_id):
        raise PathTraversalError("pipeline_id contains unsupported path characters")
    return pipeline_id


def _safe_params(value: object) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SchemaOpenError("node params must be a mapping")
    safe: dict[str, Any] = {}
    for key, item in value.items():
        safe_key = _safe_text(key, label="node param key", max_length=160)
        if isinstance(item, str):
            safe[safe_key] = _safe_text(item, label="node param value", max_length=4_000)
        elif isinstance(item, (int, float, bool)) or item is None:
            safe[safe_key] = item
        else:
            raise SchemaOpenError("node params may only contain scalar values")
    return safe


def _workflows_root() -> Path:
    """Return the absolute path to the workflows storage directory.

    Resolves relative to the repository root (two levels above this file:
    vetinari/workflows/builder.py -> vetinari/workflows/ -> vetinari/ -> repo).
    Creates the directory if it does not exist.

    Returns:
        Path to the ``outputs/workflows`` directory.
    """
    root = OUTPUTS_DIR / _WORKFLOWS_DIR_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PipelineNode:
    """One step in a pipeline graph.

    Attributes:
        node_id: Unique identifier within the pipeline.
        node_type: Semantic category, e.g. ``"task"``, ``"decision"``,
            ``"loop"``.
        params: Arbitrary key/value parameters for this step.  Schema is
            determined by the execution dispatch layer, not validated here.
    """

    node_id: str  # unique within the owning pipeline
    node_type: str  # e.g. "task", "decision", "loop"
    params: dict[str, Any]  # step-specific parameters; schema is caller-owned

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"PipelineNode(node_id={self.node_id!r}, node_type={self.node_type!r})"


@dataclass(frozen=True, slots=True)
class PipelineEdge:
    """A directed connection between two nodes.

    Attributes:
        from_node: ``node_id`` of the source node.
        to_node: ``node_id`` of the target node.
        condition: Optional guard expression evaluated at runtime.  ``None``
            means the edge is unconditional.
    """

    from_node: str  # source node_id
    to_node: str  # target node_id
    condition: str | None = None  # guard expression; None = unconditional

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"PipelineEdge(from_node={self.from_node!r}, to_node={self.to_node!r}, condition={self.condition!r})"


@dataclass(frozen=True, slots=True)
class Pipeline:
    """Immutable pipeline graph value object.

    Attributes:
        pipeline_id: UUID-based identifier, unique across all stored pipelines.
        name: Human-readable display name.
        nodes: Ordered tuple of pipeline steps.
        edges: Ordered tuple of directed connections.
        created_at: ISO 8601 UTC timestamp of initial creation.
        updated_at: ISO 8601 UTC timestamp of most recent save.
    """

    pipeline_id: str  # uuid4 hex string
    name: str  # human-readable display name
    nodes: tuple[PipelineNode, ...]  # pipeline steps
    edges: tuple[PipelineEdge, ...]  # directed connections
    created_at: str  # ISO 8601 UTC
    updated_at: str  # ISO 8601 UTC

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            f"Pipeline(pipeline_id={self.pipeline_id!r}, name={self.name!r},"
            f" nodes={len(self.nodes)}, edges={len(self.edges)})"
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def create_pipeline(
    name: str,
    nodes: list[PipelineNode],
    edges: list[PipelineEdge],
) -> Pipeline:
    """Construct a new Pipeline value object with a fresh ID and timestamps.

    Does not persist to disk.  Call :func:`save_pipeline` to store the result.

    Args:
        name: Human-readable pipeline name.  Must be non-empty.
        nodes: Ordered list of pipeline steps.
        edges: Directed connections between nodes.

    Returns:
        A new immutable :class:`Pipeline` instance.

    Raises:
        ValueError: If ``name`` is empty or ``nodes`` is empty.
    """
    if not name or not name.strip():
        raise ValueError("Pipeline name must be non-empty")
    if not nodes:
        raise ValueError("Pipeline must contain at least one node")

    now = datetime.now(timezone.utc).isoformat()
    safe_nodes = tuple(
        PipelineNode(
            node_id=_safe_text(node.node_id, label="node_id", max_length=160),
            node_type=_safe_text(node.node_type, label="node_type", max_length=120),
            params=_safe_params(node.params),
        )
        for node in nodes
    )
    safe_edges = tuple(
        PipelineEdge(
            from_node=_safe_text(edge.from_node, label="from_node", max_length=160),
            to_node=_safe_text(edge.to_node, label="to_node", max_length=160),
            condition=_safe_text(edge.condition, label="condition", max_length=500) if edge.condition else None,
        )
        for edge in edges
    )
    pipeline = Pipeline(
        pipeline_id=uuid.uuid4().hex,
        name=_safe_text(name.strip(), label="pipeline name", max_length=200),
        nodes=safe_nodes,
        edges=safe_edges,
        created_at=now,
        updated_at=now,
    )
    logger.debug("Created pipeline %s (%s)", pipeline.pipeline_id, pipeline.name)
    return pipeline


def save_pipeline(pipeline: Pipeline) -> Path:
    """Persist a pipeline to ``outputs/workflows/<pipeline_id>.yaml``.

    Uses an atomic temp-file + rename pattern so partial writes are never
    observed by readers.  Overwrites any existing file with the same ID.

    Args:
        pipeline: The pipeline to persist.

    Returns:
        The absolute :class:`~pathlib.Path` of the written YAML file.

    Raises:
        OSError: If the file cannot be written (e.g. permissions).
    """
    root = _workflows_root()
    dest = confine_to_root(root, f"{_safe_pipeline_id(pipeline.pipeline_id)}.yaml")
    payload = _pipeline_to_dict(pipeline)

    # Atomic write: write to a sibling temp file, then rename.
    tmp = dest.with_suffix(".yaml.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            yaml.safe_dump(payload, fh, allow_unicode=True, sort_keys=False)
        tmp.replace(dest)
    except Exception:
        # Clean up partial temp file so it cannot be mistaken for valid data.
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise

    logger.debug("Saved pipeline %s to %s", pipeline.pipeline_id, dest)
    return dest


def load_pipeline(pipeline_id: str) -> Pipeline:
    """Read and deserialise a pipeline from disk.

    Args:
        pipeline_id: The pipeline's UUID hex string.

    Returns:
        The deserialised :class:`Pipeline` instance.

    Raises:
        FileNotFoundError: If no pipeline with ``pipeline_id`` exists on disk.
        ValueError: If the YAML payload cannot be deserialised into a valid
            Pipeline (e.g. missing required fields).
    """
    root = _workflows_root()
    path = confine_to_root(root, f"{_safe_pipeline_id(pipeline_id)}.yaml")
    if not path.exists():
        raise FileNotFoundError(
            f"Pipeline '{pipeline_id}' not found at {path} — "
            "verify the pipeline_id or call list_pipelines() to enumerate stored pipelines"
        )

    with path.open("r", encoding="utf-8") as fh:
        payload = yaml.safe_load(fh)

    if not isinstance(payload, dict):
        raise ValueError(
            f"Pipeline file {path} contains invalid YAML — expected a mapping, got {type(payload).__name__}"
        )

    pipeline = _pipeline_from_dict(payload, path)
    logger.debug("Loaded pipeline %s from %s", pipeline_id, path)
    return pipeline


def list_pipelines() -> list[str]:
    """Return the pipeline IDs of all pipelines stored on disk.

    Enumerates ``outputs/workflows/*.yaml`` and returns the stem of each
    filename as a pipeline ID.  The list is sorted for deterministic output.

    Returns:
        Sorted list of pipeline ID strings.  Empty list if no pipelines exist.
    """
    root = _workflows_root()
    ids = sorted(p.stem for p in root.glob("*.yaml") if not p.name.endswith(".yaml.tmp"))
    logger.debug("list_pipelines found %d pipelines in %s", len(ids), root)
    return ids


def validate_pipeline(pipeline: Pipeline) -> list[str]:
    """Validate a pipeline graph and return a list of error messages.

    Checks performed (in order):
    1. At least one node is present.
    2. No duplicate ``node_id`` values.
    3. Every edge's ``from_node`` and ``to_node`` reference a known node.
    4. No cycles (depth-first search over the directed adjacency graph).

    Args:
        pipeline: The pipeline to validate.

    Returns:
        A list of human-readable error strings.  An empty list means the
        pipeline is valid.
    """
    errors: list[str] = []

    # -- Rule 1: at least one node --
    if not pipeline.nodes:
        errors.append("Pipeline has no nodes; at least one node is required")
        return errors  # remaining checks assume nodes exist

    # -- Rule 2: no duplicate node IDs --
    seen_ids: set[str] = set()
    for node in pipeline.nodes:
        if node.node_id in seen_ids:
            errors.append(f"Duplicate node_id: '{node.node_id}'")
        seen_ids.add(node.node_id)

    # -- Rule 3: edge endpoint integrity --
    known_ids = {node.node_id for node in pipeline.nodes}
    for edge in pipeline.edges:
        if edge.from_node not in known_ids:
            errors.append(f"Edge references unknown from_node '{edge.from_node}'; known nodes: {sorted(known_ids)}")
        if edge.to_node not in known_ids:
            errors.append(f"Edge references unknown to_node '{edge.to_node}'; known nodes: {sorted(known_ids)}")

    # -- Rule 4: cycle detection (DFS on adjacency list) --
    # Build adjacency list only from valid edges so cycle check is meaningful
    # even when edge-integrity errors were reported above.
    adjacency: dict[str, list[str]] = {node.node_id: [] for node in pipeline.nodes}
    for edge in pipeline.edges:
        if edge.from_node in adjacency:
            adjacency[edge.from_node].append(edge.to_node)

    if _has_cycle(adjacency):
        errors.append("Pipeline graph contains a cycle; pipelines must be acyclic (DAGs)")

    return errors


# ---------------------------------------------------------------------------
# Execution dispatch hook
# ---------------------------------------------------------------------------


def dispatch_pipeline(pipeline: Pipeline, inputs: dict[str, Any]) -> dict[str, Any]:
    """Entry point for pipeline execution dispatch.

    Validates the pipeline and returns a dispatch receipt. The current builder
    surface is a graph contract and wiring proof; it does not register or invoke
    node executors.

    Args:
        pipeline: The pipeline to execute.  Must be valid (see
            :func:`validate_pipeline`).
        inputs: Key/value inputs passed to the first node.

    Returns:
        A receipt dict with ``pipeline_id``, ``name``, ``node_count``, and
        ``status`` keys.

    Raises:
        ValueError: If the pipeline fails validation.
    """
    errors = validate_pipeline(pipeline)
    if errors:
        raise ValueError(f"Cannot dispatch invalid pipeline '{pipeline.pipeline_id}': " + "; ".join(errors))

    logger.info(
        "Dispatching pipeline %s (%s) with %d nodes",
        pipeline.pipeline_id,
        pipeline.name,
        len(pipeline.nodes),
    )
    return {
        "pipeline_id": pipeline.pipeline_id,
        "name": pipeline.name,
        "node_count": len(pipeline.nodes),
        "status": "dispatched",
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _has_cycle(adjacency: dict[str, list[str]]) -> bool:
    """Return True if the directed graph described by ``adjacency`` has a cycle.

    Uses iterative DFS with three-colour marking (white/grey/black) to detect
    back-edges without recursion, avoiding stack-overflow on large graphs.

    Args:
        adjacency: Mapping from node_id to list of successor node_ids.

    Returns:
        ``True`` if a directed cycle exists, ``False`` otherwise.
    """
    # Colour states: 0 = unvisited, 1 = in current DFS path, 2 = fully explored
    colour: dict[str, int] = dict.fromkeys(adjacency, 0)

    for start in adjacency:
        if colour[start] != 0:
            continue
        # Iterative DFS: stack holds (node, iterator_over_neighbours)
        stack: list[tuple[str, Any]] = [(start, iter(adjacency.get(start, [])))]
        colour[start] = 1
        while stack:
            node, neighbours = stack[-1]
            try:
                neighbour = next(neighbours)
                if neighbour not in colour:
                    # Edge to a node outside the adjacency set (unknown target);
                    # the edge-integrity check already reported this, skip here.
                    continue
                if colour[neighbour] == 1:
                    return True  # back edge → cycle
                if colour[neighbour] == 0:
                    colour[neighbour] = 1
                    stack.append((neighbour, iter(adjacency.get(neighbour, []))))
            except StopIteration:
                colour[node] = 2
                stack.pop()
    return False


def _pipeline_to_dict(pipeline: Pipeline) -> dict[str, Any]:
    """Serialise a Pipeline to a plain dict suitable for YAML safe_dump.

    Args:
        pipeline: The pipeline to serialise.

    Returns:
        A plain-Python dict with no custom objects.
    """
    return {
        "pipeline_id": pipeline.pipeline_id,
        "name": pipeline.name,
        "created_at": pipeline.created_at,
        "updated_at": pipeline.updated_at,
        "nodes": [
            {
                "node_id": node.node_id,
                "node_type": node.node_type,
                "params": dict(node.params),
            }
            for node in pipeline.nodes
        ],
        "edges": [
            {
                "from_node": edge.from_node,
                "to_node": edge.to_node,
                "condition": edge.condition,
            }
            for edge in pipeline.edges
        ],
    }


def _pipeline_from_dict(payload: dict[str, Any], source_path: Path) -> Pipeline:
    """Deserialise a plain dict (from YAML safe_load) into a Pipeline.

    Args:
        payload: The raw dict from yaml.safe_load.
        source_path: The file the payload was loaded from (for error messages).

    Returns:
        A deserialised :class:`Pipeline` instance.

    Raises:
        ValueError: If required fields are absent or have the wrong type.
    """
    required = ("pipeline_id", "name", "created_at", "updated_at", "nodes")
    for field in required:
        if field not in payload:
            raise ValueError(f"Pipeline file {source_path} is missing required field '{field}'")
    assert_closed_schema(payload, allowed_keys=_PIPELINE_KEYS, required_keys=required)

    raw_nodes = payload.get("nodes") or []
    if not isinstance(raw_nodes, list):
        raise ValueError(f"Pipeline file {source_path}: 'nodes' must be a list, got {type(raw_nodes).__name__}")
    nodes = tuple(
        PipelineNode(
            node_id=_safe_text(n.get("node_id", ""), label="node_id", max_length=160),
            node_type=_safe_text(n.get("node_type", ""), label="node_type", max_length=120),
            params=_safe_params(n.get("params") or {}),
        )
        for n in _validate_node_payloads(raw_nodes)
    )

    raw_edges = payload.get("edges") or []
    if not isinstance(raw_edges, list):
        raise ValueError(f"Pipeline file {source_path}: 'edges' must be a list, got {type(raw_edges).__name__}")
    edges = tuple(
        PipelineEdge(
            from_node=_safe_text(e.get("from_node", ""), label="from_node", max_length=160),
            to_node=_safe_text(e.get("to_node", ""), label="to_node", max_length=160),
            condition=_safe_text(e.get("condition"), label="condition", max_length=500) if e.get("condition") else None,
        )
        for e in _validate_edge_payloads(raw_edges)
    )

    return Pipeline(
        pipeline_id=_safe_pipeline_id(payload["pipeline_id"]),
        name=_safe_text(payload["name"], label="pipeline name", max_length=200),
        nodes=nodes,
        edges=edges,
        created_at=_safe_text(payload["created_at"], label="created_at", max_length=80),
        updated_at=_safe_text(payload["updated_at"], label="updated_at", max_length=80),
    )


def _validate_node_payloads(raw_nodes: list[Any]) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for item in raw_nodes:
        if not isinstance(item, dict):
            raise SchemaOpenError("pipeline nodes must be mappings")
        assert_closed_schema(item, allowed_keys=_NODE_KEYS, required_keys=("node_id", "node_type"))
        nodes.append(item)
    return nodes


def _validate_edge_payloads(raw_edges: list[Any]) -> list[dict[str, Any]]:
    edges: list[dict[str, Any]] = []
    for item in raw_edges:
        if not isinstance(item, dict):
            raise SchemaOpenError("pipeline edges must be mappings")
        assert_closed_schema(item, allowed_keys=_EDGE_KEYS, required_keys=("from_node", "to_node"))
        edges.append(item)
    return edges


__all__ = [
    "Pipeline",
    "PipelineEdge",
    "PipelineNode",
    "create_pipeline",
    "dispatch_pipeline",
    "list_pipelines",
    "load_pipeline",
    "save_pipeline",
    "validate_pipeline",
]
