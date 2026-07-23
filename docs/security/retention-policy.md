# Retention Policy

Authoritative machine-readable policy: `docs/security/data-inventory.json`.

| Policy | Maximum Retention | Mechanism |
|---|---:|---|
| `training-records-30d` | 30 days | Training collector persists redacted prompt/response text and privacy metadata; operators delete stale JSONL records after 30 days and exports derive from redacted records. |
| `memory-active-plus-tombstone` | 365 days | Memory `forget()` tombstones records and overwrites content with `[forgotten]`; `compact_memories()` physically removes tombstoned rows. |
| `logs-14d` | 14 days | SSE buffer is bounded; file logs rely on operator rotation after `LogRecord` redaction. |
| `support-bundles-operator-owned` | operator-owned | Bundle builder redacts UTF-8 content before zip write; operators delete exported bundles when no longer needed. |
| `outputs-scratch-14d` | 14 days | `TrainingScheduler` idle maintenance sweeps stale files under `outputs/`. |
| `network-evidence-30d` | 30 days | Network state is redacted before persistence; stale JSON artifacts are removable by operator state cleanup. |
| `trace-exports-14d` | 14 days | GenAI trace exporter redacts tool payloads at record/export time; export files are operator-managed. |
| `chat-exports-operator-owned` | operator-owned | Chat export and attachment response bytes are redacted before leaving the app; operators own exported copies. |

## Active Erasure Contract

Tombstone-only deletion is acceptable only for active memory rows before compaction because `forget()` overwrites raw content, clears summary and metadata, and active APIs filter `forgotten = 0`. Physical deletion is required during compaction. For `outputs/`, the runtime cleanup path is not a doc-only promise: idle scheduler maintenance calls the scratch sweep and removes stale files beyond the stated TTL.
