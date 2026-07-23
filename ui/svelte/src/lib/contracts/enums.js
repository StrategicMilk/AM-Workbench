/**
 * FSA-1528 - agent.state from /api/v1/workbench/:id/managed-agents.
 * @readonly
 */
export const AgentState = Object.freeze({
  ACTIVE: 'active',
  PAUSED: 'paused',
  RETIRED: 'retired',
  INITIALIZING: 'initializing',
  ERROR: 'error',
});

/**
 * FSA-0157/FSA-1540 - project.status and project progress payloads.
 * @readonly
 */
export const ProjectStatus = Object.freeze({
  COMPLETE: 'complete',
  DONE: 'done',
  RUNNING: 'running',
  FAILED: 'failed',
  PENDING: 'pending',
});

/**
 * FSA-1540/FSA-1539 - task status fields surfaced in project and agent views.
 * @readonly
 */
export const TaskStatus = Object.freeze({
  ACTIVE: 'active',
  BLOCKED: 'blocked',
  RUNNING: 'running',
  COMPLETED: 'completed',
  SUCCEEDED: 'succeeded',
  FAILED: 'failed',
});

/**
 * FSA-1534 - readiness.state from update safety and onboarding payloads.
 * @readonly
 */
export const ReadinessState = Object.freeze({
  READY: 'ready',
  BLOCKED: 'blocked',
  DEGRADED: 'degraded',
  UNKNOWN: 'unknown',
});

/**
 * FSA-1535 - work graph state from workflow builder payloads.
 * @readonly
 */
export const GraphState = Object.freeze({
  READY: 'ready',
  BUILDING: 'building',
  ERROR: 'error',
});

/**
 * FSA-1536 - guardrail check.status payload values.
 * @readonly
 */
export const CheckStatus = Object.freeze({
  PASSING: 'passing',
  FAILING: 'failing',
  SKIPPED: 'skipped',
  PENDING: 'pending',
});

/**
 * FSA-1530/FSA-1533 - preference/runtime UX card.status payload values.
 * @readonly
 */
export const CardStatus = Object.freeze({
  ALL: 'all',
  ALLOWED: 'allowed',
  ACTIVE: 'active',
  BLOCKED: 'blocked',
  PROPOSED: 'proposed',
  REJECTED: 'rejected',
  FORGOTTEN: 'forgotten',
  DECAYED: 'decayed',
  REVOKED: 'revoked',
  EXPIRED: 'expired',
  UNAVAILABLE: 'unavailable',
  REPLAY_MISMATCH: 'replay_mismatch',
  APPROVAL_REQUIRED: 'approval_required',
  DEGRADED: 'degraded',
  STALE: 'stale',
});

/**
 * FSA-1529 - migration item.risk payload values.
 * @readonly
 */
export const MigrationRisk = Object.freeze({
  LOW: 'low',
  MEDIUM: 'medium',
  HIGH: 'high',
  CRITICAL: 'critical',
  CONFLICT: 'conflict',
  RISKY_TOOL: 'risky_tool',
  SECRET: 'secret',
  UNAVAILABLE: 'unavailable',
  CORRUPT: 'corrupt',
});

/**
 * FSA-1532 - resource lease.status payload values.
 * @readonly
 */
export const LeaseStatus = Object.freeze({
  ACTIVE: 'active',
  APPROVED: 'approved',
  RELEASED: 'released',
  EXPIRED: 'expired',
  REVOKED: 'revoked',
});

/**
 * Training run status values emitted by training and eval surfaces.
 * @readonly
 */
export const TrainingStatus = Object.freeze({
  RUNNING: 'running',
  COMPLETED: 'completed',
  FAILED: 'failed',
  QUEUED: 'queued',
  REJECTED: 'rejected',
});

/**
 * FSA-1396 - plan execution status aliases.
 * @readonly
 */
export const PlanStatus = Object.freeze({
  EXECUTING: 'executing',
  RUNNING: 'running',
  IN_PROGRESS: 'in_progress',
  PAUSED: 'paused',
  COMPLETED: 'completed',
  COMPLETE: 'complete',
  FAILED: 'failed',
  CANCELLED: 'cancelled',
});

/**
 * FSA-1525 - rigor_required values from sensitive workflow decisions.
 * @readonly
 */
export const RigorRequired = Object.freeze({
  CHECK_IT_CAREFULLY: 'check_it_carefully',
  MAKE_IT_REUSABLE: 'make_it_reusable',
  HELP_ME_THINK: 'help_me_think',
});

/**
 * FSA-1410 - branch kind values used by creative and project flow payloads.
 * @readonly
 */
export const BranchKind = Object.freeze({
  CANON: 'canon',
  EXPLORATORY: 'exploratory',
});

/**
 * FSA-1410 - creative continuity violation kinds.
 * @readonly
 */
export const CreativeViolationKind = Object.freeze({
  TRAIT_CONTRADICTION: 'trait-contradiction',
  TIMELINE_EVENT_UNKNOWN: 'timeline-event-unknown',
});

/**
 * FSA-1316 - intake depth values from intake request payloads.
 * @readonly
 */
export const IntakeDepth = Object.freeze({
  QUICK: 'quick',
  STANDARD: 'standard',
  DEEP: 'deep',
});

/**
 * FSA-1316 - intake priority values from intake request payloads.
 * @readonly
 */
export const IntakePriority = Object.freeze({
  LOW: 'low',
  MEDIUM: 'medium',
  HIGH: 'high',
  CRITICAL: 'critical',
});

/**
 * FSA-1397 - run kind values used by console filters and run history.
 * @readonly
 */
export const RunKind = Object.freeze({
  AGENT_RUN: 'agent_run',
  TRAINING_RUN: 'training_run',
  EVAL_RUN: 'eval_run',
  GATEWAY_REQUEST: 'gateway_request',
  TRAINING: 'training',
  EVAL: 'eval',
  AUDIT: 'audit',
});

/**
 * FSA-0343 - gateway policy decision kind values.
 * @readonly
 */
export const GatewayDecisionKind = Object.freeze({
  ROUTE: 'route',
  CACHE: 'cache',
  BUDGET: 'budget',
  GUARDRAIL_PRE: 'guardrail_pre',
  GUARDRAIL_POST: 'guardrail_post',
});

/**
 * FSA-1405 - automation trigger source payload values.
 * @readonly
 */
export const TriggerSource = Object.freeze({
  SCHEDULE: 'schedule',
  EVENT: 'event',
  MANUAL: 'manual',
  WEBHOOK: 'webhook',
});

/**
 * FSA-1405 - automation builder trigger source payload values.
 * @readonly
 */
export const AutomationTriggerSource = Object.freeze({
  FILE_CHANGE: 'file_change',
  SOURCE_STALENESS: 'source_staleness',
  NEW_TRACE: 'new_trace',
  FAILED_EVAL: 'failed_eval',
  NEW_MODEL: 'new_model',
  DATASET_DRIFT: 'dataset_drift',
  BENCHMARK_CHANGE: 'benchmark_change',
  COST_THRESHOLD: 'cost_threshold',
  ANNOTATION_QUEUE: 'annotation_queue',
  TRAINING_COMPLETION: 'training_completion',
  CRON: 'cron',
});

/**
 * FSA-1405 - automation failure policy payload values.
 * @readonly
 */
export const FailurePolicy = Object.freeze({
  PROPOSE_ONLY: 'propose_only',
});

/**
 * FSA-1531 - graph node kind values from workbench graph query payloads.
 * @readonly
 */
export const GraphNodeKind = Object.freeze({
  TASK: 'task',
  AGENT: 'agent',
  MEMORY: 'memory',
  ARTIFACT: 'artifact',
  WORKFLOW: 'workflow',
  PROPOSAL: 'proposal',
  EVAL: 'eval',
  RUN: 'run',
  ASSET: 'asset',
  AUTOMATION: 'automation',
});

/**
 * FSA-1531 - saved graph-query view identifiers.
 * @readonly
 */
export const GraphQueryView = Object.freeze({
  FULL_CROSS_OBJECT_GRAPH: 'full_cross_object_graph',
  STALE_EVIDENCE_BLOCKED_PROMOTIONS: 'stale_evidence_blocked_promotions',
  FAILURE_SHARED_SOURCE_REVISION: 'failure_shared_source_revision',
  ROUTE_COST_WITHOUT_QUALITY_GAIN: 'route_cost_without_quality_gain',
  AUTOMATION_CHURN_WITHOUT_ADOPTION: 'automation_churn_without_adoption',
});

/**
 * FSA-7786 - update channel identifiers.
 * @readonly
 */
export const UpdateChannel = Object.freeze({
  STABLE: 'stable',
  BETA: 'beta',
  CANARY: 'canary',
});

/**
 * FSA-1532 - resource cockpit action identifiers.
 * @readonly
 */
export const ResourceAction = Object.freeze({
  CANCEL: 'cancel',
});

/**
 * FSA-1407/FSA-1411 - executable tool surface kinds shared with
 * vetinari.workbench.tool_trust.contracts.ToolSurfaceKind.
 * @readonly
 */
export const ToolSurfaceKind = Object.freeze({
  MCP_SERVER: 'mcp_server',
  SHELL_COMMAND: 'shell_command',
  BROWSER_AUTOMATION: 'browser_automation',
  CONNECTOR: 'connector',
  SKILL: 'skill',
  AUTOMATION: 'automation',
  LOCAL_HELPER: 'local_helper',
});

/**
 * FSA-1537 - training context kind values.
 * @readonly
 */
export const TrainingContextKind = Object.freeze({
  SKILL: 'skill',
  FEEDBACK: 'feedback',
  CORRECTION: 'correction',
});
