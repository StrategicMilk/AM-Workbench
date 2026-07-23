-- Vetinari Plan Mode - SQL Schema
-- This file contains the SQL schema for the Plan Mode memory store
-- Primary store: SQLite (recommended for production)
-- JSON fallback: vetinari_memory.json (for development/prototyping)

-- vetinari:add-column-if-missing PlanHistory retention_expires_at DATETIME
-- vetinari:add-column-if-missing SubtaskMemory retention_expires_at DATETIME
-- vetinari:add-column-if-missing ModelPerformance retention_expires_at DATETIME

-- ==========================================
-- PlanHistory Table
-- ==========================================
-- Stores all plan records including goals, status, risk scores, and metadata

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
    privacy_envelope_json TEXT NOT NULL DEFAULT '{}',
    subject_id TEXT,
    retention_expires_at DATETIME,
    risk_score REAL DEFAULT 0.0,
    dry_run INTEGER DEFAULT 0,
    auto_approved INTEGER DEFAULT 0
);

-- ==========================================
-- SubtaskMemory Table
-- ==========================================
-- Stores subtask details including assignments, outcomes, and rationales

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
    privacy_envelope_json TEXT NOT NULL DEFAULT '{}',
    subject_id TEXT,
    retention_expires_at DATETIME,
    domain TEXT,
    quality_score REAL DEFAULT 0.0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (plan_id) REFERENCES PlanHistory(plan_id)
);

-- ==========================================
-- ModelPerformance Table
-- ==========================================
-- Tracks model performance metrics for memory-informed selection

CREATE TABLE IF NOT EXISTS ModelPerformance (
    model_id TEXT NOT NULL,
    task_type TEXT NOT NULL,
    success_rate REAL DEFAULT 0.0,
    avg_latency REAL DEFAULT 0.0,
    total_uses INTEGER DEFAULT 0,
    privacy_envelope_json TEXT NOT NULL DEFAULT '{}',
    retention_expires_at DATETIME,
    last_used_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (model_id, task_type)
);

-- ==========================================
-- Indexes for Performance
-- ==========================================

-- Index on plan_id for fast subtask lookups
CREATE INDEX IF NOT EXISTS idx_subtask_plan_id ON SubtaskMemory(plan_id);

-- Index on created_at for retention policy queries
CREATE INDEX IF NOT EXISTS idx_plan_created_at ON PlanHistory(created_at);

-- Indexes on explicit retention expiries for privacy-envelope cleanup
CREATE INDEX IF NOT EXISTS idx_plan_retention_expires_at ON PlanHistory(retention_expires_at);
CREATE INDEX IF NOT EXISTS idx_subtask_retention_expires_at ON SubtaskMemory(retention_expires_at);
CREATE INDEX IF NOT EXISTS idx_model_perf_retention_expires_at ON ModelPerformance(retention_expires_at);

-- Index on domain for domain-specific queries
CREATE INDEX IF NOT EXISTS idx_subtask_domain ON SubtaskMemory(domain);

-- Index on status for filtering
CREATE INDEX IF NOT EXISTS idx_plan_status ON PlanHistory(status);
CREATE INDEX IF NOT EXISTS idx_subtask_status ON SubtaskMemory(status);

-- ==========================================
-- Example Queries
-- ==========================================

-- Get all plans (ordered by creation date)
-- SELECT * FROM PlanHistory ORDER BY created_at DESC LIMIT 10;

-- Get all subtasks for a specific plan
-- SELECT * FROM SubtaskMemory WHERE plan_id = 'plan_abc123' ORDER BY depth, subtask_id;

-- Get model performance for a specific task type
-- SELECT * FROM ModelPerformance WHERE task_type = 'coding' ORDER BY success_rate DESC;

-- Prune old plans (older than 90 days)
-- DELETE FROM SubtaskMemory WHERE plan_id IN (SELECT plan_id FROM PlanHistory WHERE created_at < datetime('now', '-90 days'));
-- DELETE FROM PlanHistory WHERE created_at < datetime('now', '-90 days');

-- Get plan statistics
-- SELECT
--     COUNT(*) as total_plans,
--     SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed_plans,
--     AVG(risk_score) as avg_risk_score
-- FROM PlanHistory;

-- ==========================================
-- Migration from JSON to SQLite
-- ==========================================
-- If migrating from JSON fallback to SQLite:

-- 1. Create tables using the CREATE TABLE statements above
-- 2. Parse the JSON file and insert records:
--
-- INSERT INTO PlanHistory (plan_id, plan_version, goal, created_at, updated_at, status, plan_json, chosen_plan_id, plan_justification, privacy_envelope_json, subject_id, retention_expires_at, risk_score, dry_run, auto_approved)
-- VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
--
-- INSERT INTO SubtaskMemory (subtask_id, plan_id, parent_subtask_id, description, depth, status, assigned_model_id, outcome, duration_seconds, cost_estimate, rationale, privacy_envelope_json, subject_id, retention_expires_at, domain, created_at, updated_at)
-- VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);

-- ==========================================
-- Retention Policy
-- ==========================================
-- The memory.py module automatically prunes plans older than PLAN_RETENTION_DAYS (default 90)
-- To manually prune old records, run:
--
-- DELETE FROM SubtaskMemory WHERE plan_id IN (
--     SELECT plan_id FROM PlanHistory WHERE created_at < datetime('now', '-90 days')
-- );
-- DELETE FROM PlanHistory WHERE created_at < datetime('now', '-90 days');

-- ==========================================
-- Backup and Restore
-- ==========================================
-- To backup the database:
-- sqlite3 vetinari_memory.db ".backup vetinari_memory_backup.db"

-- To restore from backup:
-- sqlite3 vetinari_memory.db ".restore vetinari_memory_backup.db"

-- To export to SQL dump:
-- sqlite3 vetinari_memory.db ".dump" > vetinari_memory_dump.sql
