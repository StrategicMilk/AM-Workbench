# Cloud-augmented Pondering

This document describes the cloud-augmented Ponder mechanism within AM Workbench, including how local models and cloud providers (HF Inference, Replicate, Claude, Gemini) are scored, how latency and caching are handled, and how auditing is performed to ensure end-to-end traceability.

## Overview
- Ponder is the model-selection orchestration engine used to pick best-fit models for each subtask.
- Cloud augmentation adds external provider relevance signals to the ranking, expanding beyond local GGUF models.
- All cloud providers are token-based, enabled via environment variables, with graceful fallbacks when tokens are missing or providers are unavailable.

## Architecture and Data Model
- Subtask-level audit data: `ponder_ranking`, `ponder_scores`, `ponder_used`
- Plan-wide ponder pass: a single pass executed after decomposition, applying cloud-augmented rankings to all subtasks
- Cloud providers are surfaced in the model pool and in the model search layer; results are normalized to a common ModelCandidate schema for consistent ranking
- Per-provider latency and caching: TTL-based caching per provider to reduce cloud-call overhead

## Token and Security Model
- Tokens for provider access come from environment variables:
  - `HF_HUB_TOKEN` - HuggingFace
  - `REPLICATE_API_TOKEN` - Replicate
  - `ANTHROPIC_API_KEY` - Claude (Anthropic), with `CLAUDE_API_KEY` accepted only as a legacy fallback
  - `GEMINI_API_KEY` - Gemini (Google)
- No tokens are logged or returned in API responses
- Fallbacks are used if tokens are missing, with cloud augmentation disabled for those providers

## Flow and Interactions
- **Phase 1**: Pre-selection of top-3 models per subtask using both local and cloud candidates
- **Phase 2**: Re-score incorporating cloud signals; auto-revert to new top-1 if ranking changes.
- Current Ponder execution is an in-process helper. Do not claim async plan-wide
  status APIs unless the route implementation and route tests are restored.

## Observability and Metrics
- Latency per provider (`cloud_latency_ms`)
- Number of cloud calls and TTL cache hits/misses
- Ponder duration per plan
- Audit fields populated for each subtask

## Rollout and Governance
- Feature flags:
  - `ENABLE_PONDER_MODEL_DISCOVERY` - Enable/disable model-discovery scoring
  - `PONDER_CLOUD_WEIGHT` - Weight for cloud signals (default: 0.20)
  - `PLAN_WIDE_PONDER_ASYNC` - Async plan-wide ponder
- Observability dashboards to monitor provider health and ponder progression
- Rollback: disable cloud augmentation if issues arise; preserve ponder audit history

## Implementation Guidance
- Ensure canonical key names for context length across all providers (`context_length`)
- Add provider health endpoints and token-health checks for operators
- Use mocks in tests for cloud adapters to achieve deterministic CI
