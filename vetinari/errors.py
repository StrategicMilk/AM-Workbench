"""Error remediation mapping — actionable guidance for common Vetinari errors.

Maps error patterns to human-readable remediation steps. Used by CLI error
handlers and the troubleshooting system to give users actionable next steps
instead of cryptic stack traces.

This is the error recovery layer: Agent Pipeline → Quality Gate →
**Error Recovery** → User Output.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ErrorRemediation:
    """A mapping from an error pattern to actionable remediation steps.

    Attributes:
        pattern: Regex pattern matching the error message.
        title: Short human-readable error title.
        explanation: What went wrong in plain English.
        steps: Ordered list of remediation steps the user should try.
        severity: How urgent this is (info, warning, error, critical).
    """

    pattern: str
    title: str
    explanation: str
    steps: list[str]
    severity: str = "error"

    def __repr__(self) -> str:
        return "ErrorRemediation(...)"


# ── Error remediation registry ───────────────────────────────────────────────
# Each entry maps a regex pattern to actionable guidance.

ERROR_REMEDIATIONS: list[ErrorRemediation] = [
    # Model / Inference
    ErrorRemediation(
        pattern=r"(?i)local inference.*unreachable|UNREACHABLE",
        title="Local inference unreachable",
        explanation=(
            "The configured local backend has no usable model. Diagnosis depends on which "
            "backend the request targeted: llama-cpp needs GGUF files on disk; "
            "vLLM/SGLang/NIM need a reachable endpoint plus a resolvable HuggingFace model; "
            "faster-whisper/ComfyUI need their backend-native model assets; "
            "hosted providers need the relevant API key."
        ),
        steps=[
            "llama-cpp: check VETINARI_MODELS_DIR points to a directory with .gguf files; verify llama-cpp-python is installed",
            "vLLM/SGLang/NIM: confirm the endpoint is reachable and VETINARI_VLLM_MODEL (or equivalent) resolves on the backend",
            "faster-whisper / ComfyUI: confirm the optional dependency group is installed and the model asset is present or reachable",
            "Hosted providers (Anthropic/OpenAI/Gemini/Cohere/HuggingFace/Replicate via LiteLLM): confirm the relevant API key env var is set",
            "Run 'vetinari models scan' to list locally discovered llama-cpp models",
            "Run 'vetinari init' to re-probe hardware and select a backend",
        ],
    ),
    ErrorRemediation(
        pattern=r"(?i)model not found|model.*not.*loaded",
        title="Model not found",
        explanation="The requested model ID doesn't match any loaded model.",
        steps=[
            "Run 'vetinari models scan' to list available models",
            "Download a model: vetinari models download --repo <id> --filename <name>",
            "Check model ID spelling in your config",
        ],
    ),
    ErrorRemediation(
        pattern=r"(?i)circuit.?breaker.*open",
        title="Circuit breaker tripped",
        explanation="The model failed repeatedly and the circuit breaker activated.",
        steps=[
            "Check model health: vetinari health",
            "Review recent errors in the logs",
            "Wait for the cooldown period, then retry",
            "If persistent, redownload the model file",
        ],
        severity="warning",
    ),
    ErrorRemediation(
        pattern=r"(?i)cuda.*out of memory|OOM|CUDA error",
        title="GPU memory exhausted",
        explanation="The model requires more VRAM than available on your GPU.",
        steps=[
            "Use a smaller quantization (Q4_K_M instead of Q6_K)",
            "Reduce n_ctx in config (e.g. 2048 instead of 4096)",
            "Set n_gpu_layers to a lower number for partial GPU offload",
            "Run 'vetinari init' for hardware-appropriate recommendations",
        ],
        severity="critical",
    ),
    # Configuration
    ErrorRemediation(
        pattern=r"(?i)inference config not found",
        title="Missing inference config",
        explanation="Config file task_inference_profiles.json not found (non-fatal).",
        steps=[
            "This is safe to ignore — sensible defaults are applied",
            "The file config/task_inference_profiles.json ships with the project — if missing, restore it with: git checkout HEAD -- config/task_inference_profiles.json",
        ],
        severity="info",
    ),
    ErrorRemediation(
        pattern=r"(?i)config.*validation.*error|ConfigurationError",
        title="Configuration error",
        explanation="Invalid value in a YAML or JSON config file.",
        steps=[
            "Run 'vetinari doctor' to identify the specific issue",
            "Check YAML syntax (spaces not tabs for indentation)",
            "Run 'vetinari config reload' after fixing",
        ],
    ),
    # Web / Dashboard
    ErrorRemediation(
        pattern=r"(?i)port.*already.*in.*use|Address already in use|EADDRINUSE",
        title="Port conflict",
        explanation="Another process is using the web server port.",
        steps=[
            "Use a different port: vetinari serve --port 5001",
            "Set VETINARI_WEB_PORT=5001 in environment",
            "Find conflicting process: lsof -i :5000 (macOS/Linux) or netstat -ano | findstr :5000 (Windows)",
        ],
        severity="warning",
    ),
    ErrorRemediation(
        pattern=r"(?i)no module named.*litestar|litestar.*not installed",
        title="Litestar not installed",
        explanation="The web API requires Litestar compatibility dependencies for the Python route stack.",
        steps=[
            "Or: pip install -e '.[web]'",
            "Run 'vetinari doctor --json' to verify web runtime dependencies",
        ],
    ),
    # Database
    ErrorRemediation(
        pattern=r"(?i)database.*locked|OperationalError.*locked",
        title="Database locked",
        explanation="Multiple processes are trying to access the SQLite database.",
        steps=[
            "Ensure only one Vetinari instance is running",
            "Run 'vetinari doctor' to check for stale lock files",
            "Delete stale .lock files in ~/.vetinari/",
        ],
    ),
    ErrorRemediation(
        pattern=r"(?i)migration.*failed",
        title="Database migration failed",
        explanation="The database schema is outdated or corrupted.",
        steps=[
            "Run: vetinari migrate --db-path vetinari_memory.db",
            "If migration fails, backup and recreate the database",
        ],
    ),
    # Agent pipeline
    ErrorRemediation(
        pattern=r"(?i)task failed after.*retries|max retries exceeded",
        title="Task failed after retries",
        explanation="The agent couldn't produce acceptable output after maximum retries.",
        steps=[
            "Check the task description — is it clear and specific?",
            "Review Inspector rejection reasons in the logs",
            "Try a more capable model for complex tasks",
            "Decompose the task into smaller subtasks",
        ],
    ),
    ErrorRemediation(
        pattern=r"(?i)orchestrator not available|TwoLayerOrchestrator.*unavailable",
        title="Orchestrator unavailable",
        explanation="The pipeline orchestrator couldn't be initialized.",
        steps=[
            "Run 'vetinari health' to check all subsystems",
            "Verify local inference: vetinari status",
            "Check logs for initialization errors",
        ],
    ),
    # Installation
    ErrorRemediation(
        pattern=r"(?i)no module named.*vetinari|ModuleNotFoundError.*vetinari",
        title="Vetinari not installed",
        explanation="The vetinari package is not installed in the current Python environment.",
        steps=[
            "pip install -e '.'",
            "Verify: python -c 'import vetinari; print(\"OK\")'",
        ],
    ),
]

# Pre-compiled patterns for fast matching
_COMPILED_PATTERNS: list[tuple[re.Pattern, ErrorRemediation]] = [(re.compile(r.pattern), r) for r in ERROR_REMEDIATIONS]


def find_remediation(error_message: str) -> ErrorRemediation | None:
    """Find the best matching remediation for an error message.

    Args:
        error_message: The error message string to match against.

    Returns:
        The matching ErrorRemediation, or None if no pattern matches.
    """
    for compiled, remediation in _COMPILED_PATTERNS:
        if compiled.search(error_message):
            return remediation
    return None


def format_remediation(remediation: ErrorRemediation) -> str:
    """Format a remediation as a human-readable string for CLI output.

    Args:
        remediation: The ErrorRemediation to format.

    Returns:
        Multi-line string with title, explanation, and numbered steps.
    """
    lines = [
        f"\n  [{remediation.severity.upper()}] {remediation.title}",
        f"  {remediation.explanation}",
        "",
        "  What to do:",
    ]
    for i, step in enumerate(remediation.steps, 1):
        lines.append(f"    {i}. {step}")
    return "\n".join(lines)
