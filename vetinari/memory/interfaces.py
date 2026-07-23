"""Memory abstraction types for the unified local memory store.

This module provides shared data contracts and compatibility environment flags
for Vetinari's SQLite/FTS-backed memory runtime.
"""

from __future__ import annotations

import hashlib
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from vetinari.types import MemoryType
from vetinari.utils.serialization import dataclass_to_dict

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class MemoryEntry:
    """A single memory entry in the store."""

    id: str = field(default_factory=lambda: f"mem_{uuid.uuid4().hex[:8]}")
    agent: str = ""
    entry_type: MemoryType = MemoryType.DISCOVERY
    content: str = ""
    summary: str = ""
    timestamp: int = field(default_factory=lambda: int(datetime.now(timezone.utc).timestamp() * 1000))
    provenance: str = ""
    source_backends: list[str] = field(default_factory=list)
    metadata: dict[str, Any] | None = None
    # Scope hierarchy: global | project:<id> | agent:<type> | task:<id>
    scope: str = "global"
    recall_count: int = 0  # Times this memory has been retrieved — boosts Ebbinghaus retention
    last_accessed: int = 0  # Epoch-ms timestamp of most recent retrieval (0 = never accessed)
    supersedes_id: str | None = None  # ID of the memory this entry replaces (fact-graph chain)
    source_entry_ids: list[str] = field(default_factory=list)  # Full lineage for compacted entries.
    relationship_type: str | None = None  # RelationshipType value linking this to supersedes_id

    def __repr__(self) -> str:
        return f"MemoryEntry(id={self.id!r}, agent={self.agent!r}, entry_type={self.entry_type.value!r})"

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dictionary."""
        return dataclass_to_dict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MemoryEntry:
        """Reconstruct a MemoryEntry from a plain dictionary.

        Args:
            data: Dictionary produced by :meth:`to_dict` or equivalent JSON payload.

        Returns:
            A new MemoryEntry instance with fields populated from *data*.
        """
        if "entry_type" in data and isinstance(data["entry_type"], str):
            data["entry_type"] = MemoryType(data["entry_type"])
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class MemoryStats:
    """Statistics about the memory store."""

    total_entries: int = 0
    file_size_bytes: int = 0
    oldest_entry: int = 0
    newest_entry: int = 0
    entries_by_agent: dict[str, int] = field(default_factory=dict)
    entries_by_type: dict[str, int] = field(default_factory=dict)

    def __repr__(self) -> str:
        return f"MemoryStats(total_entries={self.total_entries!r}, file_size_bytes={self.file_size_bytes!r})"

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dictionary."""
        return dataclass_to_dict(self)


@dataclass(frozen=True, slots=True)
class ApprovalDetails:
    """Details about an approval decision.

    JSON Schema:
    {
        "audit_id": "string (required, auto-generated)",
        "task_id": "string (optional)",
        "plan_id": "string (required)",
        "task_type": "string (optional)",
        "approval_status": "string (required: approved/rejected/auto_approved)",
        "approver": "string (required)",
        "reason": "string (optional)",
        "risk_score": "float (optional)",
        "timestamp": "string (required, auto-generated)"
    }
    """

    audit_id: str = ""
    task_id: str | None = None
    plan_id: str = ""
    task_type: str = ""
    approval_status: str = ""  # "approved", "rejected", "auto_approved"
    approver: str = ""
    reason: str = ""
    risk_score: float | None = None
    timestamp: str = ""

    def __repr__(self) -> str:
        return (
            f"ApprovalDetails(audit_id={self.audit_id!r}, plan_id={self.plan_id!r}, "
            f"approval_status={self.approval_status!r}, approver={self.approver!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dictionary."""
        return dataclass_to_dict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ApprovalDetails:
        """Reconstruct an ApprovalDetails from a plain dictionary.

        Args:
            data: Dictionary produced by :meth:`to_dict` or equivalent JSON payload.

        Returns:
            A new ApprovalDetails instance with fields populated from *data*.
        """
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


def content_hash(content: str) -> str:
    """Calculate SHA-256 hash of content for deduplication."""
    return hashlib.sha256(content.encode()).hexdigest()


# Environment configuration
MEMORY_BACKEND_MODE = os.environ.get("MEMORY_BACKEND_MODE", "dual")
OC_MEMORY_PATH = os.environ.get("OC_MEMORY_PATH", "./.vetinari/mnemoria")
MNEMOSYNE_PATH = os.environ.get("MNEMOSYNE_PATH", "./.vetinari/mnemosyne")
MEMORY_PRIMARY_READ = os.environ.get("MEMORY_PRIMARY_READ", "oc")
MEMORY_DEDUP_ENABLED = os.environ.get("MEMORY_DEDUP_ENABLED", "true").lower() in ("1", "true", "yes")
MEMORY_MERGE_LIMIT = int(os.environ.get("MEMORY_MERGE_LIMIT", "100"))

# True when the unified memory backend (SQLite + FTS5) is available for use.
# This flag allows callers to guard optional memory logging without try/except.
DUAL_MEMORY_AVAILABLE: bool = True
