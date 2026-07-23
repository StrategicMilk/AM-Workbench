"""Unified SQLite schema text for the shared Vetinari database."""

from __future__ import annotations

_UNIFIED_SCHEMA = """
-- Durable execution: pipeline state tracking
CREATE TABLE IF NOT EXISTS execution_state (
    execution_id TEXT PRIMARY KEY,
    goal TEXT NOT NULL,
    tier TEXT NOT NULL DEFAULT 'standard',
    request_spec_json TEXT,
    pipeline_state TEXT NOT NULL DEFAULT 'executing',
    current_agent TEXT,
    task_dag_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    terminal_status TEXT,
    error TEXT
);

-- Durable execution: task checkpoint state
CREATE TABLE IF NOT EXISTS task_checkpoints (
    task_id TEXT NOT NULL,
    execution_id TEXT NOT NULL,
    agent_type TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    input_json TEXT,
    output_json TEXT,
    manifest_hash TEXT,
    started_at TEXT,
    completed_at TEXT,
    retry_count INTEGER DEFAULT 0,
    PRIMARY KEY (execution_id, task_id),
    FOREIGN KEY (execution_id) REFERENCES execution_state(execution_id)
);

-- Durable execution: paused clarification questions
CREATE TABLE IF NOT EXISTS paused_questions (
    question_id TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL,
    task_id TEXT,
    questions_json TEXT NOT NULL,
    answers_json TEXT,
    asked_at TEXT NOT NULL,
    answered_at TEXT,
    FOREIGN KEY (execution_id) REFERENCES execution_state(execution_id)
);

-- Durable execution: event history (was in-memory only)
CREATE TABLE IF NOT EXISTS execution_events (
    event_id TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    task_id TEXT,
    timestamp TEXT NOT NULL,
    data_json TEXT,
    FOREIGN KEY (execution_id) REFERENCES execution_state(execution_id)
);

-- Quality scoring: task output quality assessments
CREATE TABLE IF NOT EXISTS quality_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    model_id TEXT NOT NULL DEFAULT '',
    task_type TEXT NOT NULL DEFAULT 'general',
    overall_score REAL NOT NULL,
    completeness_score REAL DEFAULT 0.0,
    correctness_score REAL DEFAULT 0.0,
    style_score REAL DEFAULT 0.0,
    llm_calibrated INTEGER DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_quality_scores_task_id ON quality_scores(task_id);
CREATE INDEX IF NOT EXISTS idx_quality_scores_model_id ON quality_scores(model_id);

-- Episode memory: learning from past executions
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type TEXT NOT NULL,
    model_id TEXT NOT NULL,
    prompt_hash TEXT NOT NULL,
    quality_score REAL NOT NULL,
    tokens_used INTEGER NOT NULL CHECK (tokens_used > 0),
    latency_ms REAL NOT NULL CHECK (latency_ms > 0),
    success INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_episodes_task_type ON episodes(task_type);
CREATE INDEX IF NOT EXISTS idx_episodes_model_id ON episodes(model_id);

-- Dashboard alerts: system health monitoring
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_type TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'info',
    message TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    acknowledged INTEGER DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    acknowledged_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_severity ON alerts(severity);

-- Plan tracking: plan metadata and state
CREATE TABLE IF NOT EXISTS plans (
    plan_id TEXT PRIMARY KEY,
    goal TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    task_count INTEGER DEFAULT 0,
    completed_count INTEGER DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    metadata_json TEXT
);

-- Legacy Plan Mode tracking tables used by vetinari.memory.plan_tracking.
CREATE TABLE IF NOT EXISTS PlanHistory (
    plan_id TEXT PRIMARY KEY,
    plan_version INTEGER DEFAULT 1,
    goal TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    status TEXT DEFAULT 'draft',
    plan_json TEXT,
    plan_explanation_json TEXT,
    chosen_plan_id TEXT,
    plan_justification TEXT,
    risk_score REAL DEFAULT 0.0,
    dry_run BOOLEAN DEFAULT 0,
    auto_approved BOOLEAN DEFAULT 0
);

CREATE TABLE IF NOT EXISTS SubtaskMemory (
    subtask_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    parent_subtask_id TEXT,
    description TEXT NOT NULL,
    depth INTEGER DEFAULT 0,
    status TEXT DEFAULT 'pending',
    assigned_model_id TEXT,
    outcome TEXT,
    duration_seconds REAL,
    cost_estimate REAL,
    rationale TEXT,
    subtask_explanation_json TEXT,
    domain TEXT,
    quality_score REAL DEFAULT 0.0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (plan_id) REFERENCES PlanHistory(plan_id)
);

CREATE TABLE IF NOT EXISTS ModelPerformance (
    model_id TEXT NOT NULL,
    task_type TEXT NOT NULL,
    success_rate REAL DEFAULT 0.0,
    avg_latency REAL DEFAULT 0.0,
    total_uses INTEGER DEFAULT 0,
    last_used_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (model_id, task_type)
);
CREATE INDEX IF NOT EXISTS idx_subtask_plan_id ON SubtaskMemory(plan_id);
CREATE INDEX IF NOT EXISTS idx_plan_created_at ON PlanHistory(created_at);
CREATE INDEX IF NOT EXISTS idx_subtask_domain ON SubtaskMemory(domain);
CREATE INDEX IF NOT EXISTS idx_plan_status ON PlanHistory(status);
CREATE INDEX IF NOT EXISTS idx_subtask_status ON SubtaskMemory(status);

-- Unified memory: agent knowledge base
CREATE TABLE IF NOT EXISTS memory_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_type TEXT NOT NULL DEFAULT 'episodic',
    key TEXT NOT NULL,
    value_json TEXT NOT NULL,
    embedding_json TEXT,
    relevance_score REAL DEFAULT 0.0,
    access_count INTEGER DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_accessed TEXT
);
CREATE INDEX IF NOT EXISTS idx_memory_key ON memory_entries(key);
CREATE INDEX IF NOT EXISTS idx_memory_type ON memory_entries(memory_type);

-- Training data: collected training examples
CREATE TABLE IF NOT EXISTS training_data (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type TEXT NOT NULL,
    prompt TEXT NOT NULL,
    response TEXT NOT NULL,
    model_id TEXT NOT NULL DEFAULT '',
    score REAL NOT NULL DEFAULT 0.0,
    success INTEGER NOT NULL DEFAULT 1,
    is_fallback INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_training_model ON training_data(model_id);
CREATE INDEX IF NOT EXISTS idx_training_composite ON training_data(task_type, model_id, created_at);

-- Benchmark runs: suite-level aggregate metrics (from BenchmarkRunner)
CREATE TABLE IF NOT EXISTS benchmark_runs (
    run_id       TEXT PRIMARY KEY,
    suite_name   TEXT NOT NULL,
    layer        TEXT NOT NULL,
    tier         TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    total_cases  INTEGER DEFAULT 0,
    passed_cases INTEGER DEFAULT 0,
    pass_at_1    REAL DEFAULT 0.0,
    pass_k       REAL DEFAULT 0.0,
    avg_score    REAL DEFAULT 0.0,
    avg_latency  REAL DEFAULT 0.0,
    total_tokens INTEGER DEFAULT 0,
    total_cost   REAL DEFAULT 0.0,
    error_recovery_rate REAL DEFAULT 0.0,
    metadata     TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_runs_suite ON benchmark_runs(suite_name);

-- Benchmark results: per-case results linked to a run (from BenchmarkRunner)
CREATE TABLE IF NOT EXISTS benchmark_results (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL,
    case_id       TEXT NOT NULL,
    suite_name    TEXT NOT NULL,
    passed        INTEGER NOT NULL,
    score         REAL NOT NULL,
    latency_ms    REAL DEFAULT 0.0,
    tokens        INTEGER DEFAULT 0,
    cost_usd      REAL DEFAULT 0.0,
    error_recovery INTEGER DEFAULT 0,
    error         TEXT,
    output        TEXT,
    timestamp     TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES benchmark_runs(run_id)
);
CREATE INDEX IF NOT EXISTS idx_results_run ON benchmark_results(run_id);

-- Improvement log: kaizen continuous improvement tracking
CREATE TABLE IF NOT EXISTS improvement_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    improvement_type TEXT NOT NULL,
    hypothesis TEXT NOT NULL,
    metric TEXT NOT NULL DEFAULT '',
    baseline_value REAL,
    target_value REAL,
    actual_value REAL,
    status TEXT NOT NULL DEFAULT 'proposed',
    applied_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at TEXT
);

-- ADR records: architecture decision records (alongside JSON files)
CREATE TABLE IF NOT EXISTS adrs (
    adr_id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed',
    category TEXT NOT NULL DEFAULT 'architecture',
    context TEXT,
    decision TEXT,
    consequences TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT
);

-- UnifiedMemoryStore: long-term memories with content dedup
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    agent TEXT NOT NULL DEFAULT '',
    entry_type TEXT NOT NULL,
    content TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    timestamp INTEGER NOT NULL,
    provenance TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL,
    forgotten INTEGER DEFAULT 0,
    access_count INTEGER DEFAULT 0,
    quality_score REAL DEFAULT 0.0,
    importance REAL DEFAULT 0.5,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata_json TEXT,
    scope TEXT NOT NULL DEFAULT 'global',
    recall_count INTEGER DEFAULT 0,
    supersedes_id TEXT,
    relationship_type TEXT,
    last_accessed INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_mem_agent ON memories(agent);
CREATE INDEX IF NOT EXISTS idx_mem_type ON memories(entry_type);
CREATE INDEX IF NOT EXISTS idx_mem_hash ON memories(content_hash);
CREATE INDEX IF NOT EXISTS idx_mem_ts ON memories(timestamp);
CREATE INDEX IF NOT EXISTS idx_mem_forgotten ON memories(forgotten);
CREATE INDEX IF NOT EXISTS idx_mem_scope ON memories(scope);

-- UnifiedMemoryStore: per-memory embeddings (separate to keep memories table lean)
CREATE TABLE IF NOT EXISTS embeddings (
    memory_id TEXT PRIMARY KEY,
    embedding_blob BLOB NOT NULL,
    model TEXT NOT NULL,
    dimensions INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE
);

-- UnifiedMemoryStore: agent execution episode records (learning from past runs)
-- Named memory_episodes to avoid conflict with the learning episodes table above.
CREATE TABLE IF NOT EXISTS memory_episodes (
    episode_id TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL,
    task_summary TEXT NOT NULL,
    agent_type TEXT NOT NULL,
    task_type TEXT NOT NULL,
    output_summary TEXT NOT NULL,
    quality_score REAL DEFAULT 0.0,
    success INTEGER DEFAULT 0,
    model_id TEXT DEFAULT '',
    importance REAL DEFAULT 0.5,
    metadata_json TEXT,
    created_at REAL DEFAULT (unixepoch()),
    scope TEXT NOT NULL DEFAULT 'global'
);
CREATE INDEX IF NOT EXISTS idx_ep_type ON memory_episodes(task_type);
CREATE INDEX IF NOT EXISTS idx_ep_agent ON memory_episodes(agent_type);
CREATE INDEX IF NOT EXISTS idx_ep_score ON memory_episodes(quality_score);

-- UnifiedMemoryStore: episode embeddings for similarity-based recall
CREATE TABLE IF NOT EXISTS episode_embeddings (
    episode_id TEXT PRIMARY KEY,
    embedding_blob BLOB NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    dimensions INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (episode_id) REFERENCES memory_episodes(episode_id) ON DELETE CASCADE
);

-- UnifiedMemoryStore: FTS5 full-text search over memories
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    id,
    content,
    summary,
    agent,
    content=memories,
    content_rowid=rowid
);
CREATE TRIGGER IF NOT EXISTS memory_fts_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memory_fts(rowid, id, content, summary, agent)
    VALUES (NEW.rowid, NEW.id, NEW.content, NEW.summary, NEW.agent);
END;
CREATE TRIGGER IF NOT EXISTS memory_fts_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, id, content, summary, agent)
    VALUES ('delete', OLD.rowid, OLD.id, OLD.content, OLD.summary, OLD.agent);
END;
CREATE TRIGGER IF NOT EXISTS memory_fts_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, id, content, summary, agent)
    VALUES ('delete', OLD.rowid, OLD.id, OLD.content, OLD.summary, OLD.agent);
    INSERT INTO memory_fts(rowid, id, content, summary, agent)
    VALUES (NEW.rowid, NEW.id, NEW.content, NEW.summary, NEW.agent);
END;

-- RAG knowledge base: document chunks for retrieval
CREATE TABLE IF NOT EXISTS knowledge_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_path TEXT NOT NULL,
    chunk_text TEXT NOT NULL,
    chunk_index INTEGER NOT NULL DEFAULT 0,
    embedding_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_knowledge_source ON knowledge_chunks(source_path);

-- Blackboard state: persists volatile inter-agent blackboard entries across restarts
CREATE TABLE IF NOT EXISTS blackboard_state (
    project_id TEXT NOT NULL,
    state_key  TEXT NOT NULL,
    state_json TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (project_id, state_key)
);

-- Thompson Sampling arms: replaces JSON file persistence for model selection bandit
CREATE TABLE IF NOT EXISTS thompson_arms (
    arm_key     TEXT PRIMARY KEY,
    model_id    TEXT NOT NULL,
    task_type   TEXT NOT NULL,
    alpha       REAL NOT NULL DEFAULT 2.0,
    beta        REAL NOT NULL DEFAULT 2.0,
    total_pulls INTEGER NOT NULL DEFAULT 0,
    last_updated TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Conversation persistence: durable multi-turn session history
CREATE TABLE IF NOT EXISTS conversation_messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL,
    role         TEXT NOT NULL,
    content      TEXT NOT NULL,
    timestamp    REAL NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_conv_session ON conversation_messages(session_id);
CREATE INDEX IF NOT EXISTS idx_conv_session_ts ON conversation_messages(session_id, timestamp);

-- SSE event log: audit trail for server-sent events, enables replay and debugging
CREATE TABLE IF NOT EXISTS sse_event_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id   TEXT NOT NULL,
    event_type   TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    sequence_num INTEGER NOT NULL DEFAULT 0,
    emitted_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_sse_log_project ON sse_event_log(project_id, emitted_at);

-- Context window history: persists conversation messages for session resume
CREATE TABLE IF NOT EXISTS context_window_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL,
    model_id     TEXT NOT NULL,
    position     INTEGER NOT NULL,
    role         TEXT NOT NULL,
    content      TEXT NOT NULL,
    token_count  INTEGER NOT NULL DEFAULT 0,
    is_compressed INTEGER NOT NULL DEFAULT 0,
    saved_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_cwh_session ON context_window_history(session_id);

-- Kaizen improvements: continuous improvement tracking (replaces separate kaizen DB)
CREATE TABLE IF NOT EXISTS improvements (
    id TEXT PRIMARY KEY,
    hypothesis TEXT NOT NULL,
    metric TEXT NOT NULL,
    baseline_value REAL NOT NULL,
    target_value REAL NOT NULL,
    actual_value REAL,
    applied_by TEXT NOT NULL,
    created_at TEXT,
    applied_at TEXT,
    observation_window_hours INTEGER NOT NULL DEFAULT 168,
    status TEXT NOT NULL DEFAULT 'proposed',
    regression_detected INTEGER NOT NULL DEFAULT 0,
    rollback_plan TEXT NOT NULL,
    confirmed_at TEXT,
    reverted_at TEXT,
    notes TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_improvements_status ON improvements(status);

-- Kaizen observations: raw observations that feed improvement proposals
CREATE TABLE IF NOT EXISTS improvement_observations (
    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    improvement_id TEXT NOT NULL REFERENCES improvements(id),
    observed_at TEXT NOT NULL,
    metric_value REAL NOT NULL,
    sample_size INTEGER NOT NULL
);

-- Kaizen defect occurrences: recurrence tracking for quality issues
CREATE TABLE IF NOT EXISTS defect_occurrences (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    category TEXT NOT NULL,
    agent_type TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL DEFAULT '',
    task_id TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0.0
);

-- Pipeline observability: per-stage snapshots for trace replay and cost analysis (ADR-0091)
CREATE TABLE IF NOT EXISTS pipeline_traces (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id             TEXT NOT NULL,
    execution_id         TEXT NOT NULL DEFAULT '',
    step_name            TEXT NOT NULL,
    step_index           INTEGER NOT NULL DEFAULT 0,
    status               TEXT NOT NULL DEFAULT 'completed',
    input_snapshot_json  TEXT,
    output_snapshot_json TEXT,
    tokens_used          INTEGER DEFAULT 0,
    latency_ms           REAL DEFAULT 0.0,
    model_id             TEXT NOT NULL DEFAULT '',
    quality_score        REAL,
    created_at           TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_pipeline_traces_trace_id ON pipeline_traces(trace_id);
CREATE INDEX IF NOT EXISTS idx_pipeline_traces_execution_id ON pipeline_traces(execution_id);
CREATE INDEX IF NOT EXISTS idx_pipeline_traces_created_at ON pipeline_traces(created_at);
"""
