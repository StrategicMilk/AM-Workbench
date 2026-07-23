export const FEATURE_GATE_UNKNOWN_REASON = 'feature gate state unavailable';
export const GATE_KEYS = Object.freeze({
  WORKBENCH_STATUS: 'workbench_status',
  WORKFLOW_BUILDER: 'workflow_builder',
  MANAGED_AGENTS: 'managed_agents',
  MEMORY_SCOPES: 'memory_scopes',
});
export const VALID_GATE_KEYS = Object.freeze(Object.values(GATE_KEYS));
const VALID_GATE_KEY_SET = new Set(VALID_GATE_KEYS);

export function normalizeFeatureFlags(flags = {}) {
  if (!flags || typeof flags !== 'object' || Array.isArray(flags)) {
    return { ok: false, flags: {}, reason: FEATURE_GATE_UNKNOWN_REASON };
  }

  const normalized = {};
  for (const [featureId, enabled] of Object.entries(flags)) {
    if (typeof enabled !== 'boolean') {
      return { ok: false, flags: {}, reason: `feature gate ${featureId} must be boolean` };
    }
    normalized[featureId] = enabled;
  }
  return { ok: true, flags: normalized, reason: '' };
}

export class FeatureGateState {
  flags = $state({});
  unavailableReason = $state('');

  isEnabled(featureId) {
    return this.decisionFor(featureId).enabled;
  }

  decisionFor(featureId) {
    if (!featureId || typeof featureId !== 'string') {
      return {
        enabled: false,
        reason: 'feature id is required',
        receipt_id: 'rcg-0021-p05:feature-gate:blocked',
      };
    }
    if (VALID_GATE_KEY_SET.size && !VALID_GATE_KEY_SET.has(featureId)) {
      return {
        enabled: false,
        reason: 'feature id is unknown',
        receipt_id: 'rcg-0021-p05:feature-gate:blocked',
      };
    }
    if (this.unavailableReason) {
      return {
        enabled: false,
        reason: this.unavailableReason,
        receipt_id: 'rcg-0021-p05:feature-gate:blocked',
      };
    }
    return {
      enabled: this.flags?.[featureId] === true,
      reason: this.flags?.[featureId] === true ? 'enabled' : 'disabled or unknown',
      receipt_id: 'rcg-0021-p05:feature-gate:ready',
    };
  }

  setFlags(flags = {}) {
    const normalized = normalizeFeatureFlags(flags);
    this.flags = normalized.flags;
    this.unavailableReason = normalized.ok ? '' : normalized.reason;
  }
}
