"""Type definitions, enums, and data classes for the dynamic model router.

Contains all pure-data types used by ``DynamicModelRouter`` and its callers:
``TaskType`` alias, ``ModelStatus``, ``ModelCapabilities``, ``ModelInfo``,
``ModelSelection``, and ``RoutingPolicy``.  Keeping these here lets the main
router module stay focused on the routing algorithm without importing the full
type tree on every import.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from vetinari.security.fail_closed import UntrustedInputError, assert_closed_schema, sanitize_untrusted_text
from vetinari.types import GoalCategory, ModelProvider  # canonical enums from types.py
from vetinari.utils.serialization import dataclass_to_dict

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from vetinari.awareness.confidence import ConfidenceResult, UnknownSituationProtocol


# =====================================================================
# Enums
# =====================================================================

# Decision: TaskType consolidated into GoalCategory in vetinari.types (ADR-0075).
# This alias preserves backward compatibility for callers importing TaskType
# from this module.  Use GoalCategory directly in new code.
TaskType = GoalCategory

# Maps old TaskType string values to their GoalCategory equivalents.
# Used by parse_task_type() to handle legacy callers passing old-style strings.
_TASK_TYPE_COMPAT: dict[str, GoalCategory] = {
    "coding": GoalCategory.CODE,
    "analysis": GoalCategory.RESEARCH,
    "documentation": GoalCategory.DOCS,
    "data_processing": GoalCategory.DATA,
    "security_audit": GoalCategory.SECURITY,
    "image_generation": GoalCategory.IMAGE,
    "creative_writing": GoalCategory.CREATIVE,
}


def parse_task_type(raw: str) -> GoalCategory:
    """Parse a task type string into GoalCategory, handling old TaskType values.

    Args:
        raw: Task type string — accepts both old-style (``"coding"``) and
             new-style (``"code"``) values.

    Returns:
        The matching GoalCategory member.

    Raises:
        UntrustedInputError: If the task type is unknown or unsafe.
    """
    normalized = sanitize_untrusted_text(raw, max_length=128).lower()
    if normalized in _TASK_TYPE_COMPAT:
        return _TASK_TYPE_COMPAT[normalized]
    try:
        return GoalCategory(normalized)
    except ValueError:
        raise UntrustedInputError(f"unknown task type: {normalized}") from None


class ModelStatus(Enum):
    """Model availability status (from ModelRelay)."""

    AVAILABLE = "available"
    LOADING = "loading"
    UNAVAILABLE = "unavailable"


# =====================================================================
# Data classes
# =====================================================================


@dataclass
class ModelCapabilities:
    """Model capabilities and attributes."""

    # Core capabilities
    code_gen: bool = False
    reasoning: bool = False
    chat: bool = True
    creative: bool = False
    docs: bool = False
    math: bool = False
    analysis: bool = False
    summarization: bool = False

    # Technical specs
    context_length: int = 8192
    supports_functions: bool = False
    supports_vision: bool = False
    supports_json: bool = False

    # Preferred for
    preferred_for: list[str] = field(default_factory=list)

    # Tags from discovery
    tags: list[str] = field(default_factory=list)

    def __repr__(self) -> str:
        """Show key capability flags for debugging."""
        return (
            f"ModelCapabilities(code_gen={self.code_gen!r},"
            f" reasoning={self.reasoning!r},"
            f" context_length={self.context_length!r})"
        )

    @classmethod
    def from_dict(cls, data: dict) -> ModelCapabilities:
        """Create capabilities from model data dict.

        Args:
            data: Dictionary with ``capabilities``, ``tags``, ``context_len``,
                and related fields.

        Returns:
            Populated ModelCapabilities instance.
        """
        assert_closed_schema(
            data,
            allowed_keys={
                "capabilities",
                "tags",
                "context_len",
                "context_length",
                "supports_functions",
                "supports_vision",
                "supports_json",
                "preferred_for",
            },
        )
        caps = cls()

        # Extract from capabilities list
        caps_list = data.get("capabilities", [])
        if isinstance(caps_list, list):
            cap_strs = [sanitize_untrusted_text(str(c), max_length=128).lower() for c in caps_list]
            caps.code_gen = any("code" in c or "coder" in c for c in cap_strs)
            caps.reasoning = any("reason" in c for c in cap_strs)
            caps.chat = any("chat" in c for c in cap_strs)
            caps.creative = any("creative" in c or "story" in c for c in cap_strs)
            caps.docs = any("doc" in c for c in cap_strs)
            caps.math = any("math" in c or "calc" in c for c in cap_strs)
            caps.analysis = any("analysis" in c or "analyze" in c for c in cap_strs)
            caps.summarization = any("summary" in c or "summarize" in c for c in cap_strs)

        # Extract from tags
        tags = data.get("tags", [])
        if isinstance(tags, list):
            tag_strs = [sanitize_untrusted_text(str(t), max_length=128).lower() for t in tags]
            caps.tags = tag_strs

            # Infer capabilities from tags
            if any("code" in t for t in tag_strs):
                caps.code_gen = True
            if any("reason" in t for t in tag_strs):
                caps.reasoning = True
            if any("chat" in t for t in tag_strs):
                caps.chat = True

        # Technical specs
        caps.context_length = data.get("context_len", data.get("context_length", 8192))
        caps.supports_functions = data.get("supports_functions", False)
        caps.supports_vision = data.get("supports_vision", False)
        caps.supports_json = data.get("supports_json", False)

        # Preferred for
        caps.preferred_for = [
            sanitize_untrusted_text(str(item), max_length=128) for item in data.get("preferred_for", [])
        ]

        return caps

    def matches_task(self, task_type: GoalCategory) -> float:
        """Return a score (0-1) for how well this model matches a task type.

        Args:
            task_type: The GoalCategory to score capability against.

        Returns:
            Capability match score between 0.0 and 1.0.
        """
        scores = {
            GoalCategory.PLANNING: 0.3 * int(self.reasoning)
            + 0.3 * int(self.code_gen)
            + 0.2 * int(self.analysis)
            + 0.2 * int(self.docs),
            GoalCategory.RESEARCH: 0.4 * int(self.analysis) + 0.3 * int(self.reasoning) + 0.3 * int(self.code_gen),
            GoalCategory.CODE: 0.8 * int(self.code_gen) + 0.1 * int(self.reasoning) + 0.1 * int(self.chat),
            GoalCategory.CODE_REVIEW: 0.5 * int(self.code_gen) + 0.3 * int(self.reasoning) + 0.2 * int(self.analysis),
            GoalCategory.TESTING: 0.6 * int(self.code_gen) + 0.2 * int(self.reasoning) + 0.2 * int(self.docs),
            GoalCategory.DOCS: 0.5 * int(self.docs) + 0.3 * int(self.code_gen) + 0.2 * int(self.chat),
            GoalCategory.REASONING: 0.6 * int(self.reasoning) + 0.2 * int(self.analysis) + 0.2 * int(self.math),
            GoalCategory.CREATIVE: 0.6 * int(self.creative) + 0.4 * int(self.chat),
            GoalCategory.DATA: 0.5 * int(self.code_gen) + 0.3 * int(self.analysis) + 0.2 * int(self.reasoning),
            GoalCategory.WEB_SEARCH: 0.4 * int(self.analysis) + 0.3 * int(self.reasoning) + 0.3 * int(self.chat),
            GoalCategory.SUMMARIZATION: 0.6 * int(self.summarization) + 0.4 * int(self.chat),
            GoalCategory.TRANSLATION: 0.5 * int(self.chat) + 0.3 * int(self.creative) + 0.2 * int(self.reasoning),
            GoalCategory.GENERAL: 0.4 * int(self.chat) + 0.3 * int(self.code_gen) + 0.3 * int(self.reasoning),
            GoalCategory.SECURITY: 0.4 * int(self.analysis) + 0.4 * int(self.reasoning) + 0.2 * int(self.code_gen),
            GoalCategory.DEVOPS: 0.5 * int(self.code_gen) + 0.3 * int(self.reasoning) + 0.2 * int(self.chat),
            GoalCategory.IMAGE: 0.5 * int(self.creative) + 0.3 * int(self.chat) + 0.2 * int(self.reasoning),
            GoalCategory.COST_ANALYSIS: 0.4 * int(self.analysis) + 0.3 * int(self.reasoning) + 0.3 * int(self.math),
            GoalCategory.SPECIFICATION: 0.4 * int(self.docs) + 0.3 * int(self.reasoning) + 0.3 * int(self.analysis),
            GoalCategory.UI: 0.4 * int(self.creative) + 0.3 * int(self.code_gen) + 0.3 * int(self.chat),
        }
        return scores.get(task_type, 0.5)


@dataclass
class RouterModelInfo:
    """Complete model information including capabilities and performance metrics."""

    id: str
    name: str
    provider: ModelProvider = ModelProvider.LOCAL
    endpoint: str = ""
    capabilities: ModelCapabilities = field(default_factory=ModelCapabilities)

    # Resource info
    memory_gb: float = 2.0
    context_length: int = 8192

    # Performance metrics
    avg_latency_ms: float = 0.0
    success_rate: float = 1.0
    total_uses: int = 0

    # Availability
    is_available: bool = True
    last_checked: str = ""
    load_percentage: float = 0.0  # 0-100

    # Metadata
    version: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> RouterModelInfo:
        """Create RouterModelInfo from a discovery or config dictionary.

        Args:
            data: Dict with ``id``, ``name``, ``capabilities``, ``memory_gb``,
                ``context_len``, and optional ``metrics`` sub-dict.

        Returns:
            Populated RouterModelInfo instance.
        """
        assert_closed_schema(
            data,
            allowed_keys={
                "id",
                "name",
                "endpoint",
                "capabilities",
                "tags",
                "context_len",
                "context_length",
                "supports_functions",
                "supports_vision",
                "supports_json",
                "preferred_for",
                "memory_gb",
                "memory",
                "version",
                "metadata",
                "metrics",
            },
            required_keys={"id"},
        )
        info = cls(
            id=sanitize_untrusted_text(data.get("id", data.get("name", "")), max_length=512),
            name=sanitize_untrusted_text(data.get("name", data.get("id", "")), max_length=512),
            endpoint=sanitize_untrusted_text(data.get("endpoint", "local"), max_length=2048),
            memory_gb=data.get("memory_gb", data.get("memory", 2)),
            context_length=data.get("context_len", data.get("context_length", 8192)),
            version=data.get("version", ""),
            metadata=data.get("metadata", {}),
        )

        # Determine provider from model id heuristics
        id_lower = info.id.lower()
        if "gpt" in id_lower:
            info.provider = ModelProvider.OPENAI
        elif "claude" in id_lower:
            info.provider = ModelProvider.ANTHROPIC
        elif "gemini" in id_lower or "google" in id_lower:
            info.provider = ModelProvider.GOOGLE
        elif "llama" in id_lower or "mistral" in id_lower or "codellama" in id_lower:
            info.provider = ModelProvider.LOCAL
        elif "cloud:" in id_lower:
            info.provider = ModelProvider.OTHER

        # Set capabilities
        capability_keys = {
            "capabilities",
            "tags",
            "context_len",
            "context_length",
            "supports_functions",
            "supports_vision",
            "supports_json",
            "preferred_for",
        }
        info.capabilities = ModelCapabilities.from_dict({key: data[key] for key in capability_keys if key in data})

        # Set performance from metrics sub-dict
        if "metrics" in data:
            info.avg_latency_ms = data["metrics"].get("avg_latency_ms", 0)
            info.success_rate = data["metrics"].get("success_rate", 1.0)

        return info

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return (
            f"RouterModelInfo(id={self.id!r}, provider={self.provider!r},"
            f" is_available={self.is_available!r}, success_rate={self.success_rate!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize the model information to a dictionary.

        Returns:
            Dictionary containing all model fields and capabilities.
        """
        return {
            "id": self.id,
            "name": self.name,
            "provider": self.provider.value,
            "endpoint": self.endpoint,
            "capabilities": {
                "code_gen": self.capabilities.code_gen,
                "reasoning": self.capabilities.reasoning,
                "chat": self.capabilities.chat,
                "creative": self.capabilities.creative,
                "docs": self.capabilities.docs,
                "math": self.capabilities.math,
                "analysis": self.capabilities.analysis,
                "summarization": self.capabilities.summarization,
                "context_length": self.capabilities.context_length,
                "supports_functions": self.capabilities.supports_functions,
                "supports_vision": self.capabilities.supports_vision,
                "supports_json": self.capabilities.supports_json,
                "tags": self.capabilities.tags,
            },
            "memory_gb": self.memory_gb,
            "context_length": self.context_length,
            "avg_latency_ms": self.avg_latency_ms,
            "success_rate": self.success_rate,
            "total_uses": self.total_uses,
            "is_available": self.is_available,
            "last_checked": self.last_checked,
            "load_percentage": self.load_percentage,
            "version": self.version,
        }


@dataclass
class ModelSelection:
    """Result of model selection process."""

    model: RouterModelInfo
    score: float
    reasoning: str
    alternatives: list[RouterModelInfo] = field(default_factory=list)
    confidence: float = 1.0
    confidence_result: ConfidenceResult | None = None
    unknown_situations: list[UnknownSituationProtocol] = field(default_factory=list)

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return f"ModelSelection(model={self.model.id!r}, score={self.score!r}, confidence={self.confidence!r})"


# =====================================================================
# Routing policy (merged from model_relay.py)
# =====================================================================


@dataclass
class RouterTypePolicy:
    """Configurable routing policy for model selection."""

    local_first: bool = True
    privacy_weight: float = 1.0  # How strongly to prefer local/private models
    latency_weight: float = 0.5  # How much to penalize high-latency models
    cost_weight: float = 0.3  # How much to favor cheaper models
    max_cost_per_1k_tokens: float = 0.0  # Hard cost cap; 0 = no cap
    preferred_providers: list[str] = field(default_factory=list)

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return (
            f"RoutingPolicy(local_first={self.local_first!r},"
            f" privacy_weight={self.privacy_weight!r},"
            f" latency_weight={self.latency_weight!r})"
        )

    def to_dict(self) -> dict:
        """Serialize the routing policy to a dictionary.

        Returns:
            Dictionary containing all policy configuration fields.
        """
        return dataclass_to_dict(self)

    @classmethod
    def from_dict(cls, data: dict) -> RouterTypePolicy:
        """Create a RoutingPolicy from a dictionary.

        Args:
            data: Dictionary with routing policy fields.

        Returns:
            A new RoutingPolicy instance.
        """
        assert_closed_schema(
            data,
            allowed_keys={
                "local_first",
                "privacy_weight",
                "latency_weight",
                "cost_weight",
                "max_cost_per_1k_tokens",
                "preferred_providers",
            },
        )
        return cls(
            local_first=data.get("local_first", True),
            privacy_weight=data.get("privacy_weight", 1.0),
            latency_weight=data.get("latency_weight", 0.5),
            cost_weight=data.get("cost_weight", 0.3),
            max_cost_per_1k_tokens=data.get("max_cost_per_1k_tokens", 0.0),
            preferred_providers=[
                sanitize_untrusted_text(str(item), max_length=128) for item in data.get("preferred_providers", [])
            ],
        )


# ── Backward compatibility alias ──────────────────────────────────────────
ModelInfo = RouterModelInfo
