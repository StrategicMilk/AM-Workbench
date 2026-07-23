"""Vetinari Canonical Type Definitions.

====================================
Single source of truth for all shared enums and base types.
All modules should import from here rather than defining their own.
"""

from __future__ import annotations

from enum import Enum

from vetinari.types_evidence import ArtifactKind as ArtifactKind
from vetinari.types_evidence import EvidenceBasis as EvidenceBasis


class StatusEnum(str, Enum):
    """Unified task/subtask lifecycle status.

    Superset of the former TaskStatus, SubtaskStatus, and CodingTaskStatus
    enums. All task-like objects in the system use this single enum.
    PlanStatus remains separate for plan-specific lifecycle states.
    """

    PENDING = "pending"
    BLOCKED = "blocked"  # Waiting for dependencies
    READY = "ready"  # Dependencies met, awaiting execution
    ASSIGNED = "assigned"  # Assigned to a model/agent
    IN_PROGRESS = "in_progress"  # Actively being worked on
    RUNNING = "running"  # Execution in progress
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    WAITING = "waiting"  # Waiting for human input
    PAUSED = "paused"  # Mid-task pause awaiting external clarification
    SKIPPED = "skipped"  # Intentionally skipped
    ACKNOWLEDGED = "acknowledged"  # Task accepted in degraded/standalone mode


class ShardKind(str, Enum):
    """Work-instruction kinds used to select Inspector grading rubrics."""

    STANDARD = "standard"
    DISCOVERY = "discovery"
    SPIKE = "spike"
    REFACTOR = "refactor"
    MIGRATION = "migration"


class TaskKind(str, Enum):
    """Pass classification used by scaffold-then-fill execution.

    SCAFFOLD tasks create importable skeletons, IMPLEMENTATION tasks fill
    behavior into those surfaces, and VERIFICATION tasks assert the result.
    When a planner or executor cannot classify a task safely, it should use
    IMPLEMENTATION to preserve the legacy execution path.
    """

    SCAFFOLD = "scaffold"
    IMPLEMENTATION = "implementation"
    VERIFICATION = "verification"


class PlanStatus(Enum):
    """Canonical plan lifecycle status.

    Lifecycle: DRAFT → PENDING → APPROVED → EXECUTING → COMPLETED/FAILED.
    Side transitions: PAUSED (from EXECUTING), REJECTED/CANCELLED (from any).
    """

    DRAFT = "draft"
    PENDING = "pending"
    APPROVED = "approved"
    EXECUTING = "executing"  # Plan is actively being executed
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class AgentType(Enum):
    """Actor roles that produce verifiable work in Vetinari.

    The three factory-pipeline agents (ADR-0061) — FOREMAN, WORKER,
    INSPECTOR — handle every per-task decision in a project's lifetime.
    Three auxiliary runner roles (ADR-0103) — TRAINING, RELEASE,
    WORKBENCH — handle non-pipeline work that still produces a
    WorkReceipt and therefore needs an honest actor label rather than
    masquerading as a WORKER.

    - FOREMAN:   Plans, decomposes goals, orchestrates execution.
    - WORKER:    Executes all research, architecture, build, and operations tasks.
    - INSPECTOR: Reviews quality, runs security audits, generates tests.
    - TRAINING:  Training-pipeline runner (auxiliary, not a factory-pipeline agent).
    - RELEASE:   Release-doctor runner (auxiliary, not a factory-pipeline agent).
    - WORKBENCH: Workbench subsystem runner (auxiliary, not a factory-pipeline agent).

    Factory-pipeline routing, prompt selection, and inference budgeting
    only consider FOREMAN/WORKER/INSPECTOR. The auxiliary values exist
    so a receipt's actor label is true (see ADR-0103).
    """

    FOREMAN = "FOREMAN"
    WORKER = "WORKER"
    INSPECTOR = "INSPECTOR"
    TRAINING = "TRAINING"
    RELEASE = "RELEASE"
    WORKBENCH = "WORKBENCH"


class WorkerMode(str, Enum):
    """Worker runtime modes exposed through the compatibility interface."""

    BUILD = "build"
    SUGGEST = "suggest"
    CODE_DISCOVERY = "code_discovery"
    ARCHITECTURE = "architecture"


class ExecutionMode(Enum):
    """Execution modes available in Vetinari."""

    PLANNING = "planning"  # Read-only mode for analysis and planning
    EXECUTION = "execution"  # Full read/write mode for implementation
    SANDBOX = "sandbox"  # Restricted mode for untrusted code


class ModelProvider(Enum):
    """All recognized model provider types.

    Canonical enum — adapters/base.py re-exports this as ``ProviderType``
    for use within the adapter subsystem.
    """

    LOCAL = "local"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GOOGLE = "google"  # Reserved: not in adapter registry. GEMINI is the live Google-family provider.
    GEMINI = "gemini"  # Google Gemini (distinct API from Vertex AI)
    VLLM = "vllm"  # GPU-only local inference via vLLM (ADR-0084)
    NIM = "nim"  # NVIDIA NIMs inference (ADR-0084)
    SGLANG = "sglang"  # Shared-prefix local server backend (ADR-0105)
    COMFYUI = "comfyui"  # Image/video workflow backend (ADR-0108)
    FASTER_WHISPER = "faster_whisper"  # Lightweight in-process ASR backend (ADR-0110)
    AM_ENGINE = "am_engine"  # First-party single owned backend (ADR-0165)
    OTHER = "other"


class PriorityClass(str, Enum):
    """Inference priority classes for queueing and scheduling policy."""

    INTERACTIVE = "interactive"
    WORKER = "worker"
    EVAL = "eval"
    BACKGROUND = "background"


class GoalCategory(Enum):
    """9-category goal classification for agent routing.

    Inspired by Oh-My-OpenCode's task category system (visual-engineering,
    deep, quick, ultrabrain).  Each category maps to an agent + mode + model
    tier combination in the TwoLayerOrchestrator.
    """

    CODE = "code"  # implement, build, develop, fix, refactor
    RESEARCH = "research"  # research, analyze, investigate, study
    DOCS = "docs"  # document, readme, api docs, manual
    CREATIVE = "creative"  # write, story, campaign, fiction, narrative
    SECURITY = "security"  # security, audit, vulnerability, pentest
    DATA = "data"  # database, schema, migration, ETL, SQL
    DEVOPS = "devops"  # deploy, CI/CD, docker, kubernetes, pipeline
    UI = "ui"  # UI, UX, frontend, design, wireframe
    IMAGE = "image"  # logo, icon, mockup, diagram, image
    GENERAL = "general"  # fallback — routes to PLANNER for decomposition

    # Finer-grained categories (from former TaskType, ADR-0076)
    PLANNING = "planning"
    CODE_REVIEW = "code_review"
    TESTING = "testing"
    REASONING = "reasoning"
    WEB_SEARCH = "web_search"
    SUMMARIZATION = "summarization"
    TRANSLATION = "translation"
    COST_ANALYSIS = "cost_analysis"
    SPECIFICATION = "specification"


class FailureType(Enum):
    """Failure taxonomy for intelligent error handling in the execution engine.

    Classifying failures enables the orchestrator to choose the correct
    recovery strategy rather than blindly retrying or giving up.
    """

    TRANSIENT = "transient"  # Timeout, temp error -> retry same agent
    DECOMPOSITION = "decomposition"  # Too complex -> post to PLANNER for subtask split
    DELEGATION = "delegation"  # Wrong agent -> post to Blackboard for reassignment
    UNSOLVABLE = "unsolvable"  # Genuine failure -> ErrorRecoveryAgent -> user escalation
    POLICY_VIOLATION = "policy"  # Security/constraint violation -> block + report


class ThinkingMode(str, Enum):
    """Canonical thinking depth levels for skill tools.

    Controls the level of detail and depth in skill tool outputs.
    All skill tools should import this from types.py rather than
    defining their own copy.
    """

    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


class MemoryType(Enum):
    """Canonical memory entry types.

    Superset of all memory types used across the system:
    shared_memory.py, enhanced_memory.py, and core pipeline.
    """

    # Core types
    INTENT = "intent"
    DISCOVERY = "discovery"
    DECISION = "decision"
    PROBLEM = "problem"
    SOLUTION = "solution"
    PATTERN = "pattern"
    WARNING = "warning"
    SUCCESS = "success"
    REFACTOR = "refactor"
    SKILL = "skill"
    BUGFIX = "bugfix"
    FEATURE = "feature"
    APPROVAL = "approval"
    CONFIG = "config"
    ERROR = "error"
    CONTEXT = "context"
    # Plan execution types (from shared_memory)
    PLAN = "plan"
    WAVE = "wave"
    TASK = "task"
    PLAN_RESULT = "plan_result"
    WAVE_RESULT = "wave_result"
    TASK_RESULT = "task_result"
    MODEL_SELECTION = "model_selection"
    SANDBOX_EVENT = "sandbox_event"
    GOVERNANCE = "governance"
    # Enhanced memory types (from enhanced_memory)
    KNOWLEDGE = "knowledge"
    CODE = "code"
    CONVERSATION = "conversation"
    RESULT = "result"
    # User-facing types (documented in CLAUDE.md / memory CLI)
    FEEDBACK = "feedback"
    USER = "user"
    PROJECT = "project"
    REFERENCE = "reference"
    SESSION = "session"
    PRINCIPLE = "principle"  # Synthesized knowledge principle (knowledge compaction)
    RULE = "rule"  # Synthesized operational rule (knowledge compaction)


class CodingTaskType(str, Enum):
    """Types of coding tasks.

    Canonical definition used by vetinari.coding_agent.engine.
    """

    SCAFFOLD = "scaffold"
    IMPLEMENT = "implement"
    TEST = "test"
    REFACTOR = "refactor"
    REVIEW = "review"
    FIX = "fix"
    DOCUMENT = "document"


class TrainingAlgorithm(str, Enum):
    """Canonical identifiers for training loss algorithms."""

    DPO = "dpo"
    SIMPO = "simpo"


class SeverityLevel(str, Enum):
    """Issue severity levels for quality reviews.

    Canonical definition — replaces duplicates in:
    - vetinari.skills.quality_skill
    - vetinari.skills.evaluator
    """

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class QualityGrade(str, Enum):
    """Overall quality grades.

    Canonical definition — replaces duplicates in:
    - vetinari.skills.quality_skill (as QualityGrade)
    - vetinari.skills.evaluator (as QualityScore)
    """

    A = "A"
    B = "B"
    C = "C"
    D = "D"
    F = "F"


class PromptVersionStatus(str, Enum):
    """Lifecycle states for prompt A/B testing variants."""

    TESTING = "testing"
    SHADOW_TESTING = "shadow_testing"
    PROMOTED = "promoted"
    DEPRECATED = "deprecated"


class TrustTier(str, Enum):
    """Skill trust levels controlling sandbox and permission boundaries."""

    T1_UNTRUSTED = "t1_untrusted"
    T2_VERIFIED = "t2_verified"
    T3_TRUSTED = "t3_trusted"
    T4_CORE = "t4_core"


class InferenceStatus(str, Enum):
    """Status of a single inference call.

    Captures the outcome of a call to an LLM adapter so callers can
    distinguish successful responses from fallbacks and hard errors.
    """

    SUCCESS = "success"  # Normal response from the model
    FALLBACK = "fallback"  # Model unavailable — fallback content returned
    TIMEOUT = "timeout"  # Request timed out
    RATE_LIMITED = "rate_limited"  # Provider rate limit hit
    CONTEXT_OVERFLOW = "context_overflow"  # Prompt exceeded model context
    ERROR = "error"  # Unclassified hard error


class FailureCategory(str, Enum):
    """Fine-grained failure categories for error escalation routing.

    Used by ErrorClassifier to distinguish recoverable from fatal failures.
    Maps to EscalationLevel in orchestration.error_escalation.
    """

    TRANSIENT = "transient"  # Retry same agent — timeout, OOM, rate-limit
    SEMANTIC = "semantic"  # Rephrase prompt — ambiguous, bad format
    CAPABILITY = "capability"  # Reassign — wrong agent for the task
    POLICY = "policy"  # Policy violation — fail closed
    BUDGET = "budget"  # Budget exhausted — stop execution
    CIRCULAR = "circular"  # Circular dependency detected
    DATA = "data"  # Data corruption or integrity failure
    NETWORK = "network"  # Network/connectivity error
    FATAL = "fatal"  # No recovery possible — escalate to human


class PermissionTier(str, Enum):
    """Agent permission tiers for authorization checks.

    Higher tiers grant more capabilities.  TIER_0 is the most restrictive
    (read-only audit), TIER_3 is unrestricted (operator/admin level).
    """

    TIER_0 = "tier_0"  # Read-only: inspect, report, no modifications
    TIER_1 = "tier_1"  # Execution: run code, read/write files in sandbox
    TIER_2 = "tier_2"  # Orchestration: delegate, spawn sub-agents, modify plans
    TIER_3 = "tier_3"  # Admin: unrestricted, approve/reject plans, install packages


class AgentRxFailureCategory(str, Enum):
    """AgentRx 9-category failure taxonomy for structured root cause analysis."""

    PLAN_ADHERENCE_FAILURE = "plan_adherence_failure"
    HALLUCINATION = "hallucination"
    INVALID_TOOL_INVOCATION = "invalid_tool_invocation"
    MISINTERPRETATION_OF_TOOL_OUTPUT = "misinterpretation_of_tool_output"
    INTENT_PLAN_MISALIGNMENT = "intent_plan_misalignment"
    UNDER_SPECIFIED_INTENT = "under_specified_intent"
    INTENT_NOT_SUPPORTED = "intent_not_supported"
    GUARDRAILS_TRIGGERED = "guardrails_triggered"
    SYSTEM_FAILURE = "system_failure"


class ActionTier(str, Enum):
    """Three-tier action classification for human-in-the-loop governance."""

    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


class AutonomyLevel(str, Enum):
    """Five-level autonomy scale controlling how much human involvement is required.

    L0 = every action needs manual trigger.
    L4 = fully autonomous, no human in the loop.
    """

    L0_MANUAL = "L0"  # Human triggers every action manually
    L1_SUGGEST = "L1"  # System suggests, human approves via approval queue
    L2_ACT_REPORT = "L2"  # System acts then reports to human
    L3_ACT_LOG = "L3"  # System acts and logs silently (human can audit)
    L4_FULL_AUTO = "L4"  # Fully autonomous, no notification unless failure


class AutonomyMode(str, Enum):
    """Global autonomy dial controlling how aggressively the system acts.

    CONSERVATIVE: Cautious — risky actions need approval, only safe actions auto-execute.
    BALANCED: Default — moderate confidence proceeds, low confidence defers.
    AGGRESSIVE: Maximum autonomy — most actions auto-execute, only very low confidence defers.
    """

    CONSERVATIVE = "conservative"
    BALANCED = "balanced"
    AGGRESSIVE = "aggressive"


class DomainCareLevel(str, Enum):
    """Per-domain override controlling whether actions auto-execute or require review.

    Orthogonal to the global autonomy mode — a domain marked REVIEW stays at
    L0/L1 even in AGGRESSIVE mode. A domain marked AUTO follows normal
    confidence-based routing.
    """

    AUTO = "auto"  # Follow normal confidence-based routing
    REVIEW = "review"  # Always require human approval regardless of global mode


class PermissionDecision(str, Enum):
    """Result of an autonomy governor permission check."""

    APPROVE = "approve"  # Proceed with the action
    DENY = "deny"  # Block the action
    DEFER = "defer"  # Defer to human via approval queue


class NotificationPriority(str, Enum):
    """Priority tiers for notification routing.

    Determines which channels receive the notification and how urgently.
    """

    CRITICAL = "critical"  # Security alert, budget breach — ALL channels immediately
    HIGH = "high"  # Approval needed, task failed — desktop + dashboard immediately
    MEDIUM = "medium"  # Task completed, training done — dashboard + batched webhook
    LOW = "low"  # Routine completions, metrics — badge + daily digest only


class ConfidenceLevel(str, Enum):
    """Output confidence classification derived from token logprobs.

    Controls post-generation routing: proceed, refine, sample, or escalate.
    """

    HIGH = "high"  # Logprob mean above upper threshold — proceed directly
    MEDIUM = "medium"  # Between thresholds — trigger self-refinement
    LOW = "low"  # Below lower threshold — Best-of-3 sampling
    VERY_LOW = "very_low"  # Far below threshold — defer to human


class DecisionType(str, Enum):
    """Classifies what kind of decision was made in the pipeline.

    Used by the decision journal to categorize entries.
    """

    ROUTING = "routing"  # Post-generation confidence routing
    APPROVAL = "approval"  # Human approval decision
    AUTONOMY = "autonomy"  # Autonomy level change (promotion/demotion)
    QUALITY = "quality"  # Quality gate pass/fail
    ESCALATION = "escalation"  # Escalation to human
    MODEL_SELECTION = "model_selection"  # Model chosen for a task
    TASK_ROUTING = "task_routing"  # Task assigned to a specific agent or worker
    QUALITY_THRESHOLD = "quality_threshold"  # Quality threshold decision (pass/fail criteria)
    PARAMETER_TUNING = "parameter_tuning"  # Inference parameter adjustment
    CONFIG_CHANGE = "config_change"  # Runtime configuration change


class ConfidenceAction(str, Enum):
    """Post-generation routing action derived from confidence level.

    Replaces the string literals ("proceed", "refine", etc.) in RoutingDecision.
    """

    PROCEED = "proceed"  # Confidence is high — use output directly
    REFINE = "refine"  # Medium confidence — trigger self-refinement
    BEST_OF_N = "best_of_n"  # Low confidence — sample N and pick best
    DEFER_TO_HUMAN = "defer_to_human"  # Very low — defer via approval queue


class FeedbackAction(str, Enum):
    """Implicit user feedback actions for the learning subsystem.

    Tracks how a user responded to model output — accepted as-is, edited,
    regenerated, or rejected — to compute implicit quality signals.
    """

    ACCEPTED = "accepted"  # User accepted output without changes
    EDITED = "edited"  # User edited the output before using it
    REGENERATED = "regenerated"  # User asked for a new generation
    REJECTED = "rejected"  # User discarded the output entirely


class ContextQuadrant(str, Enum):
    """Johari-window-inspired quadrants for the awareness context graph.

    Each quadrant represents a different domain of contextual knowledge
    that the system tracks and exposes via ContextSnapshot.
    """

    SELF = "self"  # Internal state (loaded models, VRAM, health)
    ENVIRONMENT = "environment"  # External environment (GPU, OS, config)
    USER = "user"  # User preferences and patterns
    RELATIONSHIPS = "relationships"  # Cross-entity correlations and trends
