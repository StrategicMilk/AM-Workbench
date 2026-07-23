# AM Workbench OpenCode Integration Guide

## Overview

This document describes the integration of OpenCode's advanced LLM orchestration patterns into AM Workbench. The integration adds five major components inspired by OpenCode's architecture:

1. **Execution Context System** - Agent-based execution modes with permission enforcement
2. **Standardized Tool Interface** - Unified tool definitions with metadata and safety checks
3. **Provider Agnosticism Layer** - Multi-provider support with intelligent provider selection
4. **Enhanced CLI** - Rich user feedback and explicit mode indicators
5. **Verification Pipeline** - Comprehensive post-execution validation and security scanning

**Status:** Core patterns are integrated, but this page includes historical implementation-summary context. Trust the live module paths and CLI examples in this document over old generated line counts or removed top-level flags.

---

## Implementation Summary

### Files Created

| File | Lines | Purpose |
|------|-------|---------|
| `vetinari/execution_context.py` | 450+ | Execution modes and context management |
| `vetinari/tool_interface.py` | 550+ | Tool base class and registry |
| `vetinari/adapter_manager.py` | 600+ | Multi-provider orchestration |
| `vetinari/verification/claim_verifier.py` | current module | Fail-closed claim verification hooks for Inspector outcome signals |
| `vetinari/cli.py` and `vetinari/cli_*.py` | current package CLI | Enhanced CLI with global execution mode and subcommands |

### Capabilities Before vs. After

| Feature | Before | After |
|---------|--------|-------|
| Execution modes | 1 (execution-only) | 3 (planning, execution, sandbox) |
| Permission control | None | 12 distinct permissions |
| LLM providers | 1 (local GGUF) | 6+ with fallback |
| Tool interface | Inconsistent skills | Standardized Tool base |
| CLI feedback | Basic | Rich with visual indicators |
| Output verification | Basic | Comprehensive pipeline |
| Security scanning | External | Built-in secret detection |
| Audit trail | None | Complete operation logging |
| Provider health | Not monitored | Active health checks |
| Cost tracking | Not tracked | Per-provider metrics |

---

## 1. Execution Context System

Inspired by OpenCode's agent model (build/plan), AM Workbench supports multiple execution modes with distinct permission levels:

- **PLANNING Mode** (Read-only): For analysis, planning, and code exploration
- **EXECUTION Mode** (Full access): For implementing tasks and making changes
- **SANDBOX Mode** (Restricted): For testing untrusted code with minimal permissions

### Key Classes

#### `ExecutionMode`
```python
class ExecutionMode(Enum):
    PLANNING = "planning"  # Read-only exploration
    EXECUTION = "execution"  # Full access
    SANDBOX = "sandbox"  # Restricted testing
```

#### `ToolPermission`
```python
class ToolPermission(Enum):
    FILE_READ = "file_read"
    FILE_WRITE = "file_write"
    FILE_DELETE = "file_delete"
    BASH_EXECUTE = "bash_execute"
    PYTHON_EXECUTE = "python_execute"
    MODEL_INFERENCE = "model_inference"
    MODEL_DISCOVERY = "model_discovery"
    NETWORK_REQUEST = "network_request"
    DATABASE_WRITE = "database_write"
    MEMORY_WRITE = "memory_write"
    GIT_COMMIT = "git_commit"
    GIT_PUSH = "git_push"
```

#### `ContextManager`

Manages the execution context stack and enforces permissions.

```python
from vetinari.execution_context import get_context_manager, ExecutionMode

manager = get_context_manager()

# Switch to planning mode
with manager.temporary_mode(ExecutionMode.PLANNING):
    # Operations here run in read-only planning mode
    pass

# Check permissions
if manager.check_permission(ToolPermission.FILE_WRITE):
    # Safe to write files
    pass
```

### Permission Policies

**Planning Mode:**
- FILE_READ, MODEL_INFERENCE, MODEL_DISCOVERY, NETWORK_REQUEST (read-only)
- BASH_EXECUTE, PYTHON_EXECUTE (requires confirmation)
- FILE_WRITE, FILE_DELETE, GIT_PUSH, GIT_COMMIT (denied)

**Execution Mode:**
- All read permissions, FILE_WRITE, FILE_DELETE, BASH_EXECUTE, PYTHON_EXECUTE
- DATABASE_WRITE, MEMORY_WRITE, GIT_COMMIT
- GIT_PUSH (requires confirmation)

**Sandbox Mode:**
- FILE_READ, PYTHON_EXECUTE, MODEL_INFERENCE
- BASH_EXECUTE (requires confirmation)
- FILE_WRITE, FILE_DELETE, GIT operations, DATABASE operations (denied)

---

## 2. Standardized Tool Interface

Tools are the fundamental units of work. The `Tool` base class provides standardized metadata, parameter definitions, permission requirements, input/output validation, automatic safety checks, and audit trail recording.

### Creating a Custom Tool

```python
from vetinari.tool_interface import Tool, ToolMetadata, ToolParameter, ToolCategory, ToolResult
from vetinari.execution_context import ExecutionMode, ToolPermission


class FileReadTool(Tool):
    def __init__(self):
        metadata = ToolMetadata(
            name="read_file",
            description="Read contents of a file",
            category=ToolCategory.FILE_OPERATIONS,
            version="1.0.0",
            parameters=[
                ToolParameter(name="path", type=str, description="File path to read", required=True),
                ToolParameter(name="encoding", type=str, description="File encoding", required=False, default="utf-8"),
            ],
            required_permissions=[ToolPermission.FILE_READ],
            allowed_modes=[ExecutionMode.PLANNING, ExecutionMode.EXECUTION],
        )
        super().__init__(metadata)

    def execute(self, path: str, encoding: str = "utf-8", **kwargs) -> ToolResult:
        try:
            with open(path, "r", encoding=encoding) as f:
                content = f.read()
            return ToolResult(success=True, output=content)
        except Exception as e:
            return ToolResult(success=False, error=str(e))
```

### Tool Registry

```python
from vetinari.tool_interface import get_tool_registry

registry = get_tool_registry()
registry.register(my_tool)
all_tools = registry.list_tools()
current_mode_tools = registry.list_tools_for_mode(ExecutionMode.EXECUTION)
file_tools = registry.get_tools_by_category(ToolCategory.FILE_OPERATIONS)
```

---

## 3. Provider Agnosticism Layer

Multi-provider support with unified adapter interface, automatic discovery, health monitoring, intelligent model selection, provider fallback, and cost/performance metrics tracking.

### Supported Providers

- OpenAI (GPT-4, GPT-3.5, etc.)
- Anthropic Claude
- Google Gemini
- Cohere
- Local GGUF models via llama-cpp-python
- Hugging Face / Replicate (extensible)

### Usage

```python
from vetinari.adapter_manager import get_adapter_manager
from vetinari.adapters.base import ProviderConfig, ProviderType, InferenceRequest

manager = get_adapter_manager()

# Register a provider
config = ProviderConfig(
    provider_type=ProviderType.OPENAI,
    name="openai_main",
    endpoint="https://api.openai.com/v1",
    api_key="sk-...",
)
manager.register_provider(config, "openai")

# Select best provider for task
provider_name, model_info = manager.select_provider_for_task(
    task_requirements={"required_capabilities": ["code_gen"], "input_tokens": 2000, "max_latency_ms": 10000},
    preferred_provider="openai",
)

# Run inference with automatic fallback
request = InferenceRequest(model_id="gpt-4", prompt="Write hello world", system_prompt="You are a helpful assistant")
response = manager.infer(request, provider_name=provider_name, fallback_on_error=True)
```

### Adding a New Provider

```python
from vetinari.adapters.base import (
    ProviderAdapter,
    ProviderConfig,
    ProviderType,
    ModelInfo,
    InferenceRequest,
    InferenceResponse,
)


class CustomProviderAdapter(ProviderAdapter):
    def discover_models(self) -> List[ModelInfo]: ...
    def health_check(self) -> Dict[str, Any]: ...
    def infer(self, request: InferenceRequest) -> InferenceResponse: ...
    def get_capabilities(self) -> Dict[str, List[str]]: ...


# Register
from vetinari.adapters.registry import AdapterRegistry

AdapterRegistry.register_adapter(ProviderType.CUSTOM, CustomProviderAdapter)
```

---

## 4. Enhanced CLI

AM Workbench exposes execution mode as a global flag and task execution as a `run` subcommand. Historical top-level flags such as `--task`, `--providers`, `--health-check`, and `--context` are not live entry points.

```bash
# Run in planning mode (read-only exploration)
vetinari --mode planning run --task t1

# Run in execution mode (full access)
vetinari --mode execution run --task t1

# Sandbox mode for testing
vetinari --mode sandbox run --task t1

# Check provider/system status
vetinari status

# Run health check on all providers
vetinari health

# Inspect model/provider inventory
vetinari models list
```

---

## 5. Verification Pipeline

The live verification surface is the Inspector fail-closed claim verifier. It rejects unsupported, stale, unverifiable, contradictory, bare-attested, or LLM-only high-accuracy outcome signals.

### Usage

```python
from datetime import datetime, timezone

from vetinari.agents.contracts import OutcomeSignal, Provenance, ToolEvidence
from vetinari.types import EvidenceBasis
from vetinari.verification.claim_verifier import verify_claim_fail_closed

signal = OutcomeSignal(
    passed=True,
    score=1.0,
    basis=EvidenceBasis.TOOL_EVIDENCE,
    tool_evidence=(
        ToolEvidence(
            tool_name="pytest",
            command="python -m pytest tests/test_coding_agent.py",
            exit_code=0,
            passed=True,
        ),
    ),
    provenance=Provenance(
        source="local-validation",
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
        tool_name="pytest",
    ),
)

verified = verify_claim_fail_closed(
    signal,
    claimed_path="tests/test_coding_agent.py",
    claim_text="example test exists and is covered by tool evidence",
)
if not verified.passed:
    raise RuntimeError("; ".join(verified.issues))
```

---

## Architecture

```
+----------------------------------------------------------+
|                 AM Workbench CLI                           |
|         Enhanced with Execution Modes & Status             |
+--------------------+-------------------------------------+
                     |
     +---------------+---------------+
     |               |               |
     v               v               v
+--------------+  +--------------+  +--------------+
| Execution    |  |   Tool       |  |  Adapter     |
| Context      |  |  Interface   |  |  Manager     |
|              |  |              |  |              |
| - Modes      |  | - Base Tool  |  | - Providers  |
| - Perms      |  | - Registry   |  | - Selection  |
| - Hooks      |  | - Metadata   |  | - Fallback   |
+--------------+  +--------------+  +--------------+
     |               |               |
     +---------------+---------------+
                     |
                     v
          +----------------------+
          | Verification Pipeline|
          |                      |
          | - Syntax Checking    |
          | - Security Scanning  |
          | - Import Validation  |
          | - JSON Validation    |
          +----------------------+
                     |
                     v
         +--------------------------+
         |  LLM Providers (Multi)   |
         |                          |
         | - OpenAI, Claude, Cohere |
         | - Gemini, llama-cpp-python, etc |
         +--------------------------+
```

---

## Performance

| Component | Overhead |
|-----------|----------|
| Context switching | <1ms |
| Permission checks | O(1) hash lookup |
| Provider selection | O(n*m) heuristic scoring (n providers, m models) |
| Verification | ~100-500ms for standard level |
| Memory | ~1MB for context stacks and registries |

---

## Migration Guide

### Existing Skills to Tool Interface

```python
# OLD:
class MySkill:
    def execute(self, params): ...


# NEW:
from vetinari.tool_interface import Tool, ToolMetadata, ToolCategory
from vetinari.execution_context import ExecutionMode, ToolPermission


class MyTool(Tool):
    def __init__(self):
        metadata = ToolMetadata(
            name="my_tool",
            description="My tool description",
            category=ToolCategory.CODE_EXECUTION,
            parameters=[...],
            required_permissions=[...],
            allowed_modes=[ExecutionMode.EXECUTION],
        )
        super().__init__(metadata)

    def execute(self, **kwargs):
        return ToolResult(success=True, output=result)
```

### Existing Adapters to Unified Interface

```python
# OLD:
adapter = LMStudioAdapter(...)
response = adapter.chat(...)

# NEW:
from vetinari.adapter_manager import get_adapter_manager
from vetinari.adapters.base import ProviderConfig, ProviderType, InferenceRequest

manager = get_adapter_manager()
config = ProviderConfig(provider_type=ProviderType.LOCAL, extra_config={"models_dir": "./models"})
manager.register_provider(config, "llama_cpp")
request = InferenceRequest(model_id="...", prompt="...")
response = manager.infer(request, provider_name="llama_cpp")
```

---

## Security Best Practices

1. Always use Planning mode for exploration of untrusted code
2. Enable PARANOID verification for user-generated code
3. Regularly check provider health and metrics
4. Use Secret Scanner before storing outputs in memory
5. Maintain audit trails for compliance and debugging
6. Restrict permissions by default, grant as needed
7. Use provider fallback for resilience

---

## Next Steps (Phase 2)

1. Update Orchestrator to use ExecutionContext and verification
2. Migrate existing skills to Tool interface
3. Register additional LLM providers in AdapterManager
4. Add comprehensive tests
5. Update README and documentation

---

## Related Documents

- [CONFIG.md](../reference/config.md) - Configuration reference
- [PRODUCTION.md](../reference/production.md) - Production deployment
- [README.md](../../README.md) - Project overview
