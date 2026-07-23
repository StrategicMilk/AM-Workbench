# SPDX-FileCopyrightText: 2024-2026 Vetinari Contributors
# SPDX-License-Identifier: Apache-2.0
"""Vetinari Constants.

==================
All magic numbers and default values in one place.
Import from here rather than scattering literals throughout the codebase.
"""

from __future__ import annotations

import logging
import os
import platform
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)


# ── Project Root ──────────────────────────────────────────────────────────────
_PROJECT_ROOT: Path = Path(__file__).parent.parent
PROJECT_ROOT: Path = _PROJECT_ROOT

# ── Local Inference ──────────────────────────────────────────────────────────
_DEFAULT_PROJECT_GGUF_MODELS_DIR: Path = _PROJECT_ROOT / "models"
_DEFAULT_PROJECT_NATIVE_MODELS_DIR: Path = _PROJECT_ROOT / "models" / "native"


def _home_or_temp() -> Path:
    """Return the user's home directory, falling back when the OS cannot resolve it."""
    try:
        return Path.home()
    except RuntimeError as exc:
        logger.warning("Could not determine home directory; using temp directory for Vetinari paths: %s", exc)
        return Path(tempfile.gettempdir())


def _default_gguf_models_dir() -> Path:
    if _DEFAULT_PROJECT_GGUF_MODELS_DIR.exists():
        return _DEFAULT_PROJECT_GGUF_MODELS_DIR
    return _home_or_temp() / ".vetinari" / "models"


def _default_native_models_dir() -> Path:
    if _DEFAULT_PROJECT_NATIVE_MODELS_DIR.exists():
        return _DEFAULT_PROJECT_NATIVE_MODELS_DIR
    return _home_or_temp() / ".vetinari" / "models" / "native"


DEFAULT_GGUF_MODELS_DIR: str = f"{_DEFAULT_PROJECT_GGUF_MODELS_DIR}"
DEFAULT_MODELS_DIR: str = DEFAULT_GGUF_MODELS_DIR


# ── Operator-owned cache root (no project tree fallback) ────────────────────────
# Use this for model writes to avoid polluting the project installation tree
def _get_models_cache_dir() -> str:
    """Get the operator-owned models cache directory.

    Reads VETINARI_MODELS_DIR env var, or uses ~/.cache/vetinari/models on
    Linux/Mac and %LOCALAPPDATA%/Vetinari/models on Windows.
    """
    if os.environ.get("VETINARI_MODELS_DIR"):
        return os.environ["VETINARI_MODELS_DIR"]
    if platform.system() == "Windows":
        appdata = os.environ.get("LOCALAPPDATA")
        if appdata:
            return str(Path(appdata) / "Vetinari" / "models")
    return str(_home_or_temp() / ".cache" / "vetinari" / "models")


def get_operator_models_cache_dir() -> str:
    """Return the operator-owned model cache directory on explicit request."""
    return _get_models_cache_dir()


if TYPE_CHECKING:
    OPERATOR_MODELS_CACHE_DIR: str
    DEFAULT_EMBEDDING_API_URL: str
    DEFAULT_A2A_BASE_URL: str
    DEFAULT_SEARXNG_URL: str


def __getattr__(name: str) -> str:
    if name == "OPERATOR_MODELS_CACHE_DIR":
        return get_operator_models_cache_dir()
    if name == "DEFAULT_EMBEDDING_API_URL":
        # AM Engine endpoint discovery is supervisor-owned. The environment
        # override remains only for compatibility callers that explicitly
        # manage an OpenAI-compatible endpoint.
        return os.environ.get("VETINARI_EMBEDDING_API_URL", "")
    if name == "DEFAULT_A2A_BASE_URL":
        return os.environ.get("VETINARI_A2A_BASE_URL", "http://localhost:8000")
    if name == "DEFAULT_SEARXNG_URL":
        return os.environ.get("VETINARI_SEARXNG_URL", "http://localhost:8888")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning("Invalid integer environment override %s=%r; using %d", name, raw, default)
        return default


DEFAULT_NATIVE_MODELS_DIR: str = f"{_DEFAULT_PROJECT_NATIVE_MODELS_DIR}"
DEFAULT_GPU_LAYERS: int = -1  # -1 = all layers on GPU
DEFAULT_CONTEXT_LENGTH: int = 8192

# ── Web UI ────────────────────────────────────────────────────────────────────
DEFAULT_WEB_PORT: int = 5000
DEFAULT_WEB_CLIENT_PORT: int = 3000
DEFAULT_WEB_HOST: str = "127.0.0.1"
VETINARI_DEBUG: bool = False  # Enable debug mode via VETINARI_DEBUG=1 env var

# ── Generic Timeouts (seconds) ────────────────────────────────────────────────
TIMEOUT_SHORT: int = 5  # Quick health checks, model discovery pings
TIMEOUT_MEDIUM: int = 30  # Web searches, API calls
TIMEOUT_LONG: int = 120  # Model inference calls (large models on CPU offload need >60s)
TIMEOUT_VERY_LONG: int = 180  # Long-running LLM completions
TIMEOUT_TRAINING: int = 3600  # Training runs

# ── Network / API Timeouts (seconds) ─────────────────────────────────────────
API_HEALTH_CHECK_TIMEOUT: int = 5  # LiteLLM local model health checks
MODEL_DISCOVERY_TIMEOUT: int = 30  # HuggingFace, Reddit, GitHub, PapersWithCode API searches
SUBPROCESS_TIMEOUT: int = 30  # Generic subprocess timeout (e.g., code_sandbox)
CODE_SEARCH_TIMEOUT: int = 60  # CocoIndex search subprocess
INDEX_BUILD_TIMEOUT: int = 300  # CocoIndex index_project subprocess
BATCH_RESULT_TIMEOUT: int = 300  # Waiting on a BatchProcessor future.result()
SHUTDOWN_TIMEOUT: int = 900  # AutoTuner background loop wait (15 minutes)
LOG_BACKEND_TIMEOUT: int = 10  # Datadog/external log backend HTTP requests
ALERT_SEND_TIMEOUT: int = 5  # Webhook alert POST requests
GREP_TIMEOUT: int = 10  # Ripgrep subprocess timeout
MCP_PROCESS_TIMEOUT: int = 5  # MCP server SIGTERM-to-SIGKILL grace period
GIT_OPERATION_TIMEOUT: int = 60  # Git push/pull operations
EMBEDDING_API_TIMEOUT: int = 10  # Embedding API HTTP requests (unified memory, KB)

# ── Database Timeouts ─────────────────────────────────────────────────────────
DATABASE_BUSY_TIMEOUT_MS: int = 5000  # SQLite PRAGMA busy_timeout (milliseconds)

# ── SSE / Streaming Timeouts (seconds) ────────────────────────────────────────
SSE_MESSAGE_TIMEOUT: int = 25  # SSE queue.get() timeout before heartbeat
LOG_STREAM_TIMEOUT: int = 30  # Log stream queue.get() timeout before keepalive

# ── Threading Timeouts (seconds) ──────────────────────────────────────────────
QUEUE_TIMEOUT: float = 1.0  # Worker thread queue.get() poll interval
THREAD_JOIN_TIMEOUT: float = 5.0  # Thread.join() grace period on stop/shutdown
THREAD_JOIN_TIMEOUT_SHORT: float = 2.0  # Short Thread.join() for non-critical background workers

# ── Web Search Timeouts (seconds) ────────────────────────────────────────────
WEB_SEARCH_PROBE_TIMEOUT: int = 2  # SearXNG availability probe
WEB_SEARCH_SHORT_TIMEOUT: int = 10  # DuckDuckGo, Wikipedia, arXiv
WEB_SEARCH_LONG_TIMEOUT: int = 30  # SerpAPI, Tavily, SearXNG full search

# ── Retry policies ────────────────────────────────────────────────────────────
MAX_RETRIES: int = 3
RETRY_BASE_DELAY: float = 2.0  # Seconds; used in 2^attempt backoff

# ── Circuit Breaker ──────────────────────────────────────────────────────
CIRCUIT_BREAKER_FAILURE_THRESHOLD: int = (
    5  # Consecutive failures to trip breaker (local models may fail on large contexts but succeed on smaller ones)
)
CIRCUIT_BREAKER_FAILURE_THRESHOLD_HIGH: int = 5  # Higher threshold for worker agents
CIRCUIT_BREAKER_RECOVERY_TIMEOUT: int = (
    30  # Seconds in OPEN before HALF_OPEN probe (local models recover faster than cloud APIs)
)
CIRCUIT_BREAKER_RECOVERY_TIMEOUT_LONG: int = 60  # Longer recovery for worker agents

# ── Execution ─────────────────────────────────────────────────────────────────
DEFAULT_MAX_CONCURRENT: int = 4
DEFAULT_PLAN_DEPTH_CAP: int = 16
MIN_PLAN_DEPTH: int = 12
MAX_PLAN_DEPTH: int = 16
MIN_TASKS_PER_PLAN: int = 3  # Minimum valid tasks (was 5, now 3 to allow simple goals)
MAX_TASKS_PER_PLAN: int = 20

# ── Context / Tokens ─────────────────────────────────────────────────────────
DEFAULT_CONTEXT_BUDGET: int = 80000  # Characters (~20K tokens) — modern local models handle 32K-64K context windows
DEFAULT_MAX_TOKENS: int = 8192  # Default output budget — 4K was 2023-era; 8K is minimum viable for agentic work

# ── Inference Temperature Presets ─────────────────────────────────────────────
DEFAULT_TEMPERATURE: float = 0.7  # General generation — balanced creativity/coherence
TEMPERATURE_LOW: float = 0.2  # Structured output, code, JSON, analysis — near-deterministic
TEMPERATURE_VERY_LOW: float = 0.1  # Revision/correction passes — strongly deterministic

# ── Role-Specific Token Budgets ───────────────────────────────────────────────
# Inference call budgets (output token limits for specific LLM calls)
# Calibrated to real-world agentic usage: Aider uses 8K-128K, CrewAI uses 8K,
# OpenHands recommends 16K+ for coding. The 256-2048 range was 2023-era.
MAX_TOKENS_CRITIQUE: int = 2048  # Critique with specific findings, evidence, and actionable fixes
MAX_TOKENS_REVISION: int = 8192  # Self-refinement must reproduce full revised output (often longer than original)
MAX_TOKENS_PLAN_VARIANT: int = 4096  # Plans have 5-15 tasks with IDs, deps, agents, effort, criteria
MAX_TOKENS_PROMPT_MUTATION: int = 4096  # Evolved prompts include system instructions, examples, and formatting
MAX_TOKENS_CODE_GENERATION: int = 16384  # Multi-function code with imports, docstrings, error handling, tests
MAX_TOKENS_REPO_MAP_CONTEXT: int = 8192  # Repo maps need enough structure for informed coding decisions

# Agent-level token budgets — AgentSpec declarations (model routing and context sizing)
MAX_TOKENS_FOREMAN: int = 16384  # Foreman AgentSpec: planning, coordination — needs room for complex multi-task plans
MAX_TOKENS_WORKER: int = 32768  # Worker AgentSpec: generation, reasoning — large context for complex code
MAX_TOKENS_INSPECTOR: int = 16384  # Inspector AgentSpec: reviews/validates — detailed findings need space

# Agent-level token budgets — ResourceConstraint runtime enforcement limits
# Worker's runtime cap is lower than its AgentSpec budget: avoids runaway generation in execution
RESOURCE_MAX_TOKENS_WORKER: int = 24576  # Worker ResourceConstraint: runtime enforcement ceiling

# Agent-level token budgets — SkillSpec declarations (skill output budget)
# Inspector SkillSpec allows more output than AgentSpec; detailed review outputs can be lengthy
MAX_TOKENS_INSPECTOR_SKILL: int = 24576  # Inspector SkillSpec: detailed code/security review output

# ── Truncation limits (characters) ───────────────────────────────────────────
# Sized for modern local models (32K-64K context windows). The 4K-6K char era
# was calibrated for 4K-8K token models. Modern models handle 10-20x more context.
# Generic (used across prompts, responses, context windows)
TRUNCATE_PROMPT: int = 16000  # Max chars for prompt/instruction input (~4K tokens)
TRUNCATE_RESPONSE: int = 24000  # Max chars for LLM response (~6K tokens)
TRUNCATE_CONTEXT: int = 24000  # Max chars for code review / context window (~200 lines of code)
TRUNCATE_TASK_DESC: int = 2000  # Max chars for task description with acceptance criteria and context
TRUNCATE_SNIPPET: int = 1500  # Preview/snippet for serialization, logging, and context
TRUNCATE_SEARCH_RESULT: int = 3000  # Max chars for individual search results

# Specific use-case truncation limits
TRUNCATE_CONTENT_ANALYSIS: int = 16000  # Documentation, operations, project content analysis (~4K tokens)
TRUNCATE_CODE_ANALYSIS: int = 20000  # Security audit, test generation code chunks (~150 lines)
TRUNCATE_OUTPUT_SUMMARY: int = 4000  # Agent result output summary for logging/scoring
TRUNCATE_OUTPUT_PREVIEW: int = 8000  # Cross-validation, agent graph output preview
TRUNCATE_PROMPT_TRAINING: int = 16000  # Training record prompt field (~4K tokens)
TRUNCATE_RESPONSE_TRAINING: int = 24000  # Training record response field (~6K tokens)
TRUNCATE_OTEL_OUTPUT: int = 8192  # OpenTelemetry span tool input/output attributes
TRUNCATE_CONDENSED: int = 16000  # Max chars for condensed context output (~4K tokens)
TRUNCATE_KNOWLEDGE_DOC: int = 16000  # Max chars for knowledge base document chunks (~4K tokens)

# ── Queue / Buffer Sizes ───────────────────────────────────────────────────
EVENTBUS_ASYNC_QUEUE_SIZE: int = 10000  # Async event processing queue
EVENTBUS_HISTORY_MAX_LENGTH: int = 1000  # Maximum events retained in the history ring buffer
INFERENCE_BATCHER_QUEUE_SIZE: int = 1000  # Inference batch request queue
TRAINING_COLLECTOR_QUEUE_SIZE: int = 1000  # Training data background writer queue
SSE_PROJECT_QUEUE_SIZE: int = 200  # Per-project SSE event queue
SSE_LOG_STREAM_QUEUE_SIZE: int = 1000  # Log stream SSE queue per client
SSE_VISUALIZATION_QUEUE_SIZE: int = 500  # Plan visualization SSE queue
LOG_BACKEND_BUFFER_SIZE: int = 1000  # SSE log backend in-memory buffer

# ── Polling / Startup Delays (seconds) ──────────────────────────────────────
MAIN_LOOP_POLL_INTERVAL: float = 1.0  # Dashboard/watch main loop sleep interval
VETINARI_STARTUP_DELAY: float = 1.0  # Delay after starting the native kernel/dashboard supervisor
TRAINING_SCHEDULER_DELAY: float = 2.0  # Delay for manual training scheduler cycle

# ── Quality thresholds ───────────────────────────────────────────────────────
DEFAULT_QUALITY_THRESHOLD: float = (
    0.65  # Default quality gate — calibrated to heuristic scorer range (~0.5-0.6); raise when LLM scoring active
)
HIGH_QUALITY_THRESHOLD: float = 0.85  # High-stakes quality gate ("critical" criticality)
CRITICAL_QUALITY_THRESHOLD: float = (
    0.40  # Absolute failure floor — heuristic produces ~0.493 for bad output, 0.50 barely catches it
)

# Quality gates by criticality level
QUALITY_GATE_CRITICAL: float = 0.85  # "critical" tasks — maps to HIGH_QUALITY_THRESHOLD
QUALITY_GATE_HIGH: float = 0.70  # "high" criticality tasks — calibrated to heuristic scorer range
QUALITY_GATE_MEDIUM: float = 0.55  # "medium" criticality tasks — calibrated to heuristic scorer range
QUALITY_GATE_LOW: float = 0.40  # "low" criticality tasks

# Agent-specific quality gate scores
AGENT_QUALITY_GATE_STRICT: float = 0.75  # Foreman/Inspector agents (strict mode) — achievable with heuristic scorer

# ── Model Scoring Weights (must sum to 1.0) ──────────────────────────────────
MODEL_SCORE_WEIGHT_CAPABILITY: float = 0.40  # Capability match — most important for quality
MODEL_SCORE_WEIGHT_CONTEXT: float = 0.15  # Context length fit — matters less than capability
MODEL_SCORE_WEIGHT_LATENCY: float = 0.10  # Latency score — quality > speed for most tasks
MODEL_SCORE_WEIGHT_COST: float = 0.15  # Cost per 1K tokens
MODEL_SCORE_WEIGHT_FREE_TIER: float = 0.20  # Free tier bonus — boost local model preference

# Meta-learning strategy value spaces used by Thompson strategy selection.
STRATEGY_VALUE_SPACES: dict[str, list[str | int | float]] = {
    "prompt_template_variant": ["standard", "concise", "detailed", "structured"],
    "context_window_size": [2048, 4096, 8192, 16384],
    "temperature": [0.0, 0.3, 0.5, 0.7, 1.0],
    "decomposition_granularity": ["coarse", "medium", "fine"],
}

# ── Agent Affinity Scoring Bonuses ───────────────────────────────────────────
AFFINITY_LATENCY_BONUS: float = 0.5  # Bonus when model matches latency preference
AFFINITY_UNCENSORED_BONUS: float = 1.0  # Bonus for uncensored model preference match
AFFINITY_LOCAL_BONUS: float = 0.5  # Bonus for local (private) model preference

# ── Builder Agent Quality Deductions ─────────────────────────────────────────
QUALITY_DEDUCTION_MISSING_SVG: float = 0.2  # SVG asset has no code
QUALITY_DEDUCTION_MISSING_IMAGE: float = 0.3  # PNG image file not found
QUALITY_DEDUCTION_MISSING_CODE: float = 0.3  # Scaffold code missing
QUALITY_DEDUCTION_MISSING_TESTS: float = 0.2  # Test scaffolding missing
QUALITY_DEDUCTION_MISSING_ARTIFACTS: float = 0.15  # Artifact files missing
QUALITY_DEDUCTION_MISSING_NOTES: float = 0.1  # Implementation notes missing

# ── Cross-Validation Scores ──────────────────────────────────────────────────
CROSS_VALIDATION_AGREE_SCORE: float = 1.0  # Agreement score when validator agrees
CROSS_VALIDATION_DISAGREE_SCORE: float = (
    0.3  # Agreement score when validator disagrees — stronger penalty for disagreement
)

# ── Model cache ──────────────────────────────────────────────────────────────
MODELS_CACHE_TTL: float = 60.0  # Seconds
MODEL_SEARCH_CACHE_DAYS: int = 7  # Days to cache external search results

# ── Cache and TTL ────────────────────────────────────────────────────────────
CACHE_TTL_ONE_HOUR: int = 3600  # 1 hour in seconds
CACHE_TTL_ONE_DAY: int = 86400  # 24 hours in seconds
CACHE_TTL_ONE_WEEK: int = 7 * 24 * 3600  # 7 days in seconds
CACHE_MAX_ENTRIES_PROMPT: int = 1000  # PromptCache max entries before LRU eviction
CACHE_MAX_ENTRIES_SEMANTIC: int = 1000  # SemanticCache max entries — most valuable cache, increase for more hits

# ── Training ─────────────────────────────────────────────────────────────────
MIN_TRAINING_EXAMPLES: int = 50  # Minimum records before training
TRAINING_DATA_FILE: Path = (
    _PROJECT_ROOT / ".vetinari" / "training_data.jsonl"
)  # Absolute path for cross-platform safety
TRAINING_ROTATION_LIMIT: int = 100_000  # Rotate JSONL after this many records

# ── External API Base URLs ────────────────────────────────────────────────────
ANTHROPIC_API_BASE_URL: str = "https://api.anthropic.com/v1"
OPENAI_API_BASE_URL: str = "https://api.openai.com/v1"
DATADOG_LOGS_URL: str = "https://http-intake.logs.datadoghq.com/api/v2/logs"

# ── Paths ─────────────────────────────────────────────────────────────────────
# VETINARI_DATA_ROOT is the canonical writable state root. Missing directories
# are created lazily by callers. Corrupt SQLite/preferences files fail closed in
# their owning modules rather than being silently overwritten; stale state after
# restart remains under this single root so operators can back up or repair one
# tree.


def _default_data_root() -> Path:
    if override := os.environ.get("VETINARI_DATA_ROOT"):
        return Path(override)
    if user_root := os.environ.get("VETINARI_USER_DIR"):
        return Path(user_root)
    return _home_or_temp() / ".vetinari"


VETINARI_DATA_ROOT: Path = Path(".vetinari")
CHECKPOINT_DIR: Path = VETINARI_DATA_ROOT / "vetinari_checkpoints"
OUTPUTS_DIR: Path = VETINARI_DATA_ROOT / "outputs"
PROJECTS_DIR: Path = VETINARI_DATA_ROOT / "projects"
LOGS_DIR: Path = VETINARI_DATA_ROOT / "logs"
MODEL_CACHE_DIR: Path = VETINARI_DATA_ROOT / "model_cache"
VETINARI_STATE_DIR: Path = VETINARI_DATA_ROOT / ".vetinari"

# ── Kaizen / continuous-improvement paths ─────────────────────────────────────
KAIZEN_DATA_DIR: Path = _PROJECT_ROOT / "data"  # Kaizen SQLite and JSON stores
KAIZEN_DB_PATH: Path = KAIZEN_DATA_DIR / "kaizen.db"  # Defect and improvement log DB
KAIZEN_OVERRIDES_PATH: Path = KAIZEN_DATA_DIR / "kaizen_overrides.json"  # PDCA threshold overrides

# ── Audit log paths ───────────────────────────────────────────────────────────
AUDIT_LOG_DIR: Path = LOGS_DIR / "audit"  # Directory for JSONL audit logs

# ── Search ───────────────────────────────────────────────────────────────────
DEFAULT_SEARCH_BACKEND: str = "searxng"

# ── Image generation (diffusers, in-process) ─────────────────────────────────
IMAGE_MODELS_DIR: str = f"{_PROJECT_ROOT / 'image_models'}"
IMAGE_ENABLED: bool = True
IMAGE_DEFAULT_WIDTH: int = 1024  # SDXL native resolution
IMAGE_DEFAULT_HEIGHT: int = 1024
IMAGE_DEFAULT_STEPS: int = 20
IMAGE_DEFAULT_CFG: float = 7.0

# ── AutoTuner Knobs ──────────────────────────────────────────────────────────
AUTO_TUNER_MAX_CONCURRENT: int = 8  # Maximum concurrent inference requests
AUTO_TUNER_MIN_CONCURRENT: int = 1  # Minimum concurrent inference requests
AUTO_TUNER_DEFAULT_CONCURRENT: int = 4  # Default concurrent inference requests
AUTO_TUNER_ANOMALY_THRESHOLD: float = 3.0  # Sigma threshold for anomaly detection
AUTO_TUNER_RETRY_BACKOFF_CAP: int = 30  # Maximum retry backoff in seconds
AUTO_TUNER_MIN_QUALITY_THRESHOLD: float = 0.65  # Minimum quality score for auto-tuning

# ── Decomposition Agent Knobs ────────────────────────────────────────────────
DECOMP_BREADTH_FIRST_WEIGHT: float = 0.6  # Prefer breadth over depth in decomposition
DECOMP_MIN_SUBTASK_WORDS: int = 5  # Minimum words in a subtask description
DECOMP_MAX_SUBTASKS_PER_LEVEL: int = 8  # Maximum subtasks per decomposition level
DECOMP_QUALITY_THRESHOLD: float = 0.65  # Minimum quality score to accept a subtask
DECOMP_SEED_RATE: float = 0.3  # Task seeding rate for training data collection
DECOMP_SEED_MIX: float = 0.5  # Seed mix ratio for task variety
DECOMP_DEFAULT_MAX_DEPTH: int = 14  # Default maximum decomposition depth
DECOMP_MIN_MAX_DEPTH: int = 12  # Minimum allowed decomposition depth cap
DECOMP_MAX_MAX_DEPTH: int = 16  # Maximum allowed decomposition depth cap

# ── Sandbox / Plugin Defaults ────────────────────────────────────────────────
SANDBOX_TIMEOUT: int = 300  # External plugin sandbox execution timeout (seconds)
SANDBOX_MAX_MEMORY_MB: int = 2048  # External plugin sandbox memory limit (MB)

# ── Context Compression ───────────────────────────────────────────────────────
COMPRESS_THRESHOLD_CHARS: int = 48000  # Character count above which local summarisation triggers (~12K tokens)

# ── Rework / Correction Loops ─────────────────────────────────────────────────
MAX_REWORK_CYCLES: int = 3  # Escalate to user after this many rework attempts per task

# ── Inference Response Status ────────────────────────────────────────────
INFERENCE_STATUS_OK: str = "ok"  # Successful inference response
INFERENCE_STATUS_ERROR: str = "error"  # Failed inference response
INFERENCE_STATUS_TIMEOUT: str = "timeout"  # Inference timed out
INFERENCE_STATUS_PARTIAL: str = "partial"  # Partial/incomplete response
INFERENCE_STATUS_SUCCESS: str = "success"  # Adapter-level success (maps to OK for telemetry)


# ── Lazy path getters ─────────────────────────────────────────────────────────


def get_user_dir() -> Path:
    """Return the canonical per-user Vetinari directory.

    Re-reads the ``VETINARI_USER_DIR`` environment variable on every call so
    that tests can override the path via ``monkeypatch.setenv`` without
    restarting the process.

    Returns:
        Path to the user-level Vetinari data directory.
    """
    user_dir = os.environ.get("VETINARI_USER_DIR")
    if user_dir:
        return Path(user_dir)
    return _home_or_temp() / ".vetinari"
