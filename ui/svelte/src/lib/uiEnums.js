export const WORKBENCH_QUEUE_LANES = Object.freeze(['interactive', 'hub_agent', 'training']);

export const WORKBENCH_QUEUE_LANE_LABELS = Object.freeze({
  interactive: 'Interactive',
  hub_agent: 'Hub Agent',
  training: 'Training',
});

export const WORKBENCH_PRESSURE = Object.freeze({
  GREEN: 'green',
  AMBER: 'amber',
  RED: 'red',
});

export const WORKBENCH_PRESSURE_LABELS = Object.freeze({
  [WORKBENCH_PRESSURE.GREEN]: 'OK',
  [WORKBENCH_PRESSURE.AMBER]: 'Watch',
  [WORKBENCH_PRESSURE.RED]: 'Saturated',
});

export const ADAPTIVE_TUNING_STATE = Object.freeze({
  ACTIVE: 'active',
  BLOCKED: 'blocked',
  FORGOTTEN: 'forgotten',
  PENDING: 'pending',
  REJECTED: 'rejected',
  REVOKED: 'revoked',
});

export const ADAPTIVE_TUNING_ACTIONS = Object.freeze([
  { id: 'allow', label: 'Allow', disabledIn: [ADAPTIVE_TUNING_STATE.ACTIVE, ADAPTIVE_TUNING_STATE.BLOCKED] },
  { id: 'reject', label: 'Reject', disabledIn: [ADAPTIVE_TUNING_STATE.REJECTED] },
  { id: 'edit', label: 'Edit', disabledIn: [ADAPTIVE_TUNING_STATE.FORGOTTEN, ADAPTIVE_TUNING_STATE.REVOKED] },
  { id: 'forget', label: 'Forget', disabledIn: [ADAPTIVE_TUNING_STATE.FORGOTTEN] },
  { id: 'revoke', label: 'Revoke', disabledIn: [ADAPTIVE_TUNING_STATE.REVOKED] },
  { id: 'preview', label: 'Preview', disabledIn: [] },
  { id: 'rollback', label: 'Rollback', enabledOnlyIn: [ADAPTIVE_TUNING_STATE.ACTIVE] },
  { id: 'policy_override', label: 'Policy', disabledIn: [ADAPTIVE_TUNING_STATE.ACTIVE] },
]);

export const WORKFLOW_BUILDER_BUSY_STATE = Object.freeze({
  IDLE: 'idle',
  LOADING: 'loading',
  SAVING: 'saving',
  VALIDATING: 'validating',
});

export const APPROVAL_DIFF_STATUS = Object.freeze({
  APPROVED: 'approved',
  BLOCKED: 'blocked',
  OPEN: 'open',
  READY: 'ready',
  STALE: 'stale',
});
