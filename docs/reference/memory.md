# Memory System

AM Workbench's memory system is a single `UnifiedMemoryStore` backed by SQLite with FTS5
full-text search.  It replaces the earlier dual-backend approach with one coherent store
that covers session context, long-term search, and optional embedding-based semantic recall.

## Authority And Trust

Memory, semantic search, RAG retrieval, recalled episodes, and few-shot examples are lower-priority runtime inputs. They can provide context, but they are not canonical policy, prompt rails, ADR authority, or file path authority unless promoted through a reviewed source with provenance, activation status, expiry, rollback, redaction, and retention/delete linkage.

Do not document retrieved memory as relevance-safe or policy-safe by default. If a runtime path injects memory or RAG content into prompts, the final system rails, guardrails, and Inspector gates still remain authoritative.

---

## Architecture

The store is organised into three layers:

| Layer | Storage | Purpose |
|-------|---------|---------|
| Session | In-memory `SessionContext` with bounded LRU eviction | Active task context for the current run |
| Long-term | SQLite `memories` table + FTS5 virtual table | Durable, searchable store with BM25 ranking |
| Embeddings | SQLite `embeddings` table (float32 BLOBs) | Semantic similarity via local inference |

All three layers live in the same SQLite file (WAL mode) accessed through one
`threading.RLock`.  The store uses SHA-256 content hashing to reject exact duplicates
before writing.

When `sqlite-vec` is available, a `memory_vec` virtual table enables KNN vector search.
When it is absent, the store falls back to manual cosine similarity over stored BLOBs,
and then to FTS5 if no embedding can be generated.

---

## Access Pattern

```python
from vetinari.memory.unified import get_unified_memory_store

store = get_unified_memory_store()

# FTS5 keyword search (default)
results = store.search("query text", limit=5)

# Semantic / embedding search
results = store.search("query text", limit=5, use_semantic=True)

# Reverse-chronological browse
recent = store.timeline(limit=10)

# Natural-language question (alias for semantic search, top-5)
answers = store.ask("What was decided about the retry policy?")
```

`get_unified_memory_store()` returns the process-wide singleton and is safe to call from
any thread.  Use `init_unified_memory_store(**kwargs)` in tests to swap in an isolated
instance with a private `db_path`.

---

## Data Models

### MemoryEntry

| Field | Type | Description |
|-------|------|-------------|
| `id` | `str` | Auto-generated identifier (`mem_<8-hex>`) |
| `agent` | `str` | Name of the agent that created the entry |
| `entry_type` | `MemoryType` | Category enum (see below) |
| `content` | `str` | Full body text |
| `summary` | `str` | Short summary (max 200 chars) |
| `timestamp` | `int` | Unix milliseconds (UTC) |
| `provenance` | `str` | How the entry was created (e.g. `"session_consolidation"`) |
| `scope` | `str` | Visibility scope: `global`, `project:<id>`, `agent:<type>`, `task:<id>` |
| `metadata` | `dict \| None` | Optional arbitrary JSON metadata |

`source_backends` is retained on the dataclass for backward compatibility but has no
functional role — all entries come from the single SQLite store.

### MemoryStats

Returned by `store.stats()`:

| Field | Type | Description |
|-------|------|-------------|
| `total_entries` | `int` | Count of non-forgotten entries |
| `file_size_bytes` | `int` | On-disk database size |
| `oldest_entry` | `int` | Unix ms of the oldest entry |
| `newest_entry` | `int` | Unix ms of the most recent entry |
| `entries_by_agent` | `dict[str, int]` | Per-agent counts |
| `entries_by_type` | `dict[str, int]` | Per-type counts |

### Episode

Agent execution episodes are stored separately via `store.record_episode(...)` and
recalled via `store.recall_episodes(query, k=5)`.  Each episode captures
`task_description`, `agent_type`, `task_type`, `output_summary`, `quality_score`,
`success`, and `model_id`.

---

## MemoryType Values

`MemoryType` is defined in `vetinari.types`.  The full set of recognised values:

**Core types** — `intent`, `discovery`, `decision`, `problem`, `solution`, `pattern`,
`warning`, `success`, `refactor`, `bugfix`, `feature`, `approval`, `config`, `error`,
`context`

**Plan execution types** — `plan`, `wave`, `task`, `plan_result`, `wave_result`,
`task_result`, `model_selection`, `sandbox_event`, `governance`

**Enhanced types** — `knowledge`, `code`, `conversation`, `result`

**User-facing types** — `feedback`, `user`

Always import from `vetinari.types`:

```python
from vetinari.types import MemoryType

entry = MemoryEntry(entry_type=MemoryType.DECISION, content="...")
```

---

## Storage Schema

A single SQLite database (default: `.vetinari/vetinari.db`, WAL mode) holds all tables:

| Table | Purpose |
|-------|---------|
| `memories` | Primary store - all long-term entries with content hash, previous append hash, chain hash, quality, importance |
| `memory_fts` | FTS5 virtual table over `memories(id, content, summary, agent)` |
| `embeddings` | float32 BLOB per memory; foreign-keyed to `memories` |
| `memory_vec` | sqlite-vec KNN virtual table (optional, created when sqlite-vec loads) |
| `memory_episodes` | Agent execution episode records |
| `episode_embeddings` | Embedding BLOBs for episode similarity recall |
| `episode_vec` | sqlite-vec KNN virtual table for episodes (optional) |

Key indexes: `agent`, `entry_type`, `content_hash`, `timestamp`, `forgotten`.
Each append stores `previous_content_hash` and `chain_hash` so a verifier can
detect tampering or missing rows instead of seeing only independent content
hashes.

These hash-chain fields belong to the runtime `UnifiedMemoryStore` schema. Legacy
AI-assistant memory CLI scripts are not the authority for this store and should
not be used as evidence that the runtime hash chain is present or absent.

FTS5 triggers on `memories` keep `memory_fts` up to date automatically on insert,
update, and delete.

---

## Public API Reference

| Method | Returns | Notes |
|--------|---------|-------|
| `remember(entry)` | `str` (entry ID) | Deduplicates by SHA-256, appends hash-chain fields, and generates embedding asynchronously |
| `search(query, agent?, entry_types?, limit, use_semantic)` | `list[MemoryEntry]` | FTS5 by default; embedding search when `use_semantic=True` |
| `timeline(agent?, start_time?, end_time?, limit)` | `list[MemoryEntry]` | Newest-first |
| `ask(question, agent?)` | `list[MemoryEntry]` | Top-5 semantic search |
| `get_entry(entry_id)` | `MemoryEntry \| None` | Increments `access_count` |
| `forget(entry_id, reason)` | `bool` | Soft-delete (tombstone) |
| `update_content(entry_id, new_content)` | `bool` | Replace content in place |
| `compact(max_age_days?)` | `int` (removed count) | Permanently deletes tombstoned entries |
| `export(path)` | `bool` | Writes all live entries to JSON |
| `stats()` | `MemoryStats` | Aggregate counts and disk usage |
| `embed_all()` | `int` (newly embedded) | Backfills embeddings for entries that lack them |
| `consolidate(quality_threshold)` | `int` (promoted count) | Promotes high-quality session entries to long-term |
| `record_episode(...)` | `str` (episode ID) | Persists an agent execution episode with embedding |
| `recall_episodes(query, k, ...)` | `list[Episode]` | Returns similar past episodes by embedding similarity |
| `get_failure_patterns(agent_type, task_type)` | `list[str]` | Recent failure summaries for a given agent/task combination |
| `get_episode_stats()` | `dict` | Total, successful, failed, avg quality score |

---

## Memory Consolidation

Session entries do not persist across process restarts unless explicitly consolidated.
Call `store.consolidate()` at the end of a session (or on a timer) to promote entries
whose `quality_score` meets the threshold (default `0.7`) into the long-term SQLite
store.

The consolidation pipeline:

1. Iterate all session entries.
2. Skip entries below the quality threshold.
3. Check for semantic duplicates via cosine similarity.
4. Call `store.remember()` for entries that pass both gates.
5. Evict low-importance entries when the store exceeds `VETINARI_MAX_MEMORY_ENTRIES`.

---

## Configuration

All configuration is through environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `VETINARI_MAX_MEMORY_ENTRIES` | `10000` | Maximum long-term entries before eviction |
| `VETINARI_SEMANTIC_DEDUP_THRESHOLD` | `0.85` | Cosine similarity threshold for duplicate detection |
| `VETINARI_CONSOLIDATION_QUALITY_THRESHOLD` | `0.7` | Minimum quality score for session-to-long-term promotion |
| `VETINARI_SESSION_MAX_ENTRIES` | `100` | Bounded LRU size for the session layer |
| `VETINARI_EMBEDDING_DIMENSIONS` | `768` | Embedding vector size (must match the model) |
| `VETINARI_EMBEDDING_MODEL` | `text-embedding-nomic-embed-text-v1.5` | Embedding model name sent to the local inference server |

The embedding API endpoint defaults to `DEFAULT_EMBEDDING_API_URL` from
`vetinari.constants` and can be overridden at construction time.

---

## CLI Usage

The memory CLI (`scripts/memory_cli.py`) provides direct access to the store:

```bash
# Store a new entry
python scripts/memory_cli.py store --type feedback --title "..." --content "..."

# FTS5 keyword search
python scripts/memory_cli.py search "query"

# Embedding semantic search
python scripts/memory_cli.py semantic-search "query"

# Session management
python scripts/memory_cli.py session start
python scripts/memory_cli.py session end

# Backfill embeddings for entries that lack them
python scripts/memory_cli.py embed-all

# Rebuild the FTS5 index
python scripts/memory_cli.py refresh-index

# Aggregate statistics
python scripts/memory_cli.py stats
```

The `--type` flag accepts any `MemoryType` value (e.g. `feedback`, `decision`,
`discovery`).

## RCG-0069-P02 Privacy Trust Release Gate

Memory content, summaries, provenance, embeddings, and episodes are classified
by `docs/reference/privacy-manifest.json` as `FLOW-005`. RCG-0069-P02 keeps
that memory row release-blocking until
`check_privacy_trust_release_evidence` can read the dependency closure,
`config/operability_release_contract.yaml`, and this document without
violations.

The branch proof is `tests/test_rcg_0069_p02.py`. Missing closure rows, missing
docs markers, missing evidence references, unreadable JSON, or non-terminal
status all remain fail-closed rather than being treated as documentation-only
release approval.
