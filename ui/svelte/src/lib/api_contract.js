export class ContractViolationError extends Error {
  constructor(message, details = {}) {
    super(message);
    this.name = 'ContractViolationError';
    this.code = 'contract_violation';
    this.details = details;
  }
}

export const CAPABILITY_PRODUCT_CONTRACT_ID = 'RCG-0021-P01';

export const CAPABILITY_PRODUCT_SOURCE_IDS = Object.freeze([
  'FSA-0216',
  'FSA-0217',
  'FSA-0218',
  'FSA-0219',
  'FSA-0220',
  'FSA-0221',
  'FSA-0222',
  'FSA-0223',
  'FSA-0224',
  'FSA-0225',
  'FSA-0226',
  'FSA-0227',
  'FSA-0228',
  'FSA-0229',
  'FSA-0231',
  'FSA-0232',
  'FSA-0233',
  'FSA-0234',
  'FSA-0235',
  'FSA-0236',
  'FSA-0237',
  'FSA-0238',
  'FSA-0239',
  'FSA-0240',
  'FSA-0241',
]);

export const CAPABILITY_PRODUCT_SURFACE_ROWS = Object.freeze([
  ['FSA-0216', 'ui/svelte/src/components/capabilities/InstallApprovalModal.svelte', 'ui'],
  ['FSA-0217', 'ui/svelte/src/views/ResourceCockpitView.svelte', 'ui'],
  ['FSA-0218', 'ui/svelte/src/components/chat/IntakeFlow.svelte', 'ui'],
  ['FSA-0219', 'ui/svelte/src/components/workbench/console/RunResultPanel.svelte', 'ui'],
  ['FSA-0220', 'ui/svelte/src/lib/components/help/Term.svelte', 'ui'],
  ['FSA-0221', 'ui/svelte/src/lib/components/workbench/effective-config/EffectiveConfigExplorer.svelte', 'ui'],
  ['FSA-0222', 'ui/svelte/src/lib/components/workbench/life_admin/__init__.js', 'ui'],
  ['FSA-0223', 'ui/svelte/src/lib/components/workbench/model_choices/InactiveReasonBadge.svelte', 'ui'],
  ['FSA-0224', 'ui/svelte/src/lib/components/workbench/promotions/PromotionEngine.svelte', 'ui'],
  ['FSA-0225', 'ui/svelte/src/lib/components/workbench/status/FixActionDrawer.svelte', 'ui'],
  ['FSA-0226', 'ui/svelte/src/lib/components/workbench/updates/UpdateSafetyPanel.svelte', 'ui'],
  ['FSA-0227', 'ui/svelte/src/lib/components/workbench/workflow_builder/workflow_builder_store.svelte.js', 'ui'],
  ['FSA-0228', 'ui/svelte/src/views/CapabilitiesView.svelte', 'ui'],
  ['FSA-0229', 'ui/svelte/src/views/LocalRuntimeSetup.svelte', 'ui'],
  ['FSA-0231', 'ui/svelte/src/views/WorkbenchShell.svelte', 'ui'],
  ['FSA-0232', 'vetinari/workbench/rigor/dial.py', 'runtime'],
  ['FSA-0233', 'tests/test_rcg_0021_p01.py', 'tests'],
  ['FSA-0234', 'ui/svelte/src/components/capabilities/InstallApprovalModal.svelte', 'ui'],
  ['FSA-0235', 'ui/svelte/src/views/ResourceCockpitView.svelte', 'ui'],
  ['FSA-0236', 'ui/svelte/src/components/chat/IntakeFlow.svelte', 'ui'],
  ['FSA-0237', 'ui/svelte/src/components/workbench/console/RunResultPanel.svelte', 'ui'],
  ['FSA-0238', 'ui/svelte/src/lib/components/help/Term.svelte', 'ui'],
  ['FSA-0239', 'ui/svelte/src/lib/components/workbench/effective-config/EffectiveConfigExplorer.svelte', 'ui'],
  ['FSA-0240', 'ui/svelte/src/lib/components/workbench/life_admin/__init__.js', 'ui'],
  ['FSA-0241', 'ui/svelte/src/lib/components/workbench/model_choices/InactiveReasonBadge.svelte', 'ui'],
].map(([sourceId, path, surface]) => Object.freeze({ sourceId, path, surface })));

const TERMINAL_CLOSURE_STATUSES = new Set(['resolved', 'no_change', 'waived']);

export function requireObject(value, context = 'payload') {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    throw new ContractViolationError(`${context} must be an object`, { context });
  }
  return value;
}

export function requireArray(value, context = 'payload') {
  if (!Array.isArray(value)) {
    throw new ContractViolationError(`${context} must be an array`, { context });
  }
  return value;
}

export function requireNonEmptyString(value, context = 'value') {
  if (typeof value !== 'string' || value.trim() === '') {
    throw new ContractViolationError(`${context} must be a non-empty string`, { context });
  }
  return value;
}

export function normalizeApiError(error, fallback = 'request failed') {
  if (error instanceof ContractViolationError) {
    return error;
  }
  const message = error instanceof Error ? error.message : String(error || fallback);
  return new ContractViolationError(message || fallback, { cause: message || fallback });
}

export function validateCapabilityProductClosure(payload) {
  const closure = requireObject(payload, 'capability product closure');
  if (closure.contract_id !== CAPABILITY_PRODUCT_CONTRACT_ID) {
    throw new ContractViolationError('unexpected capability product contract id', {
      expected: CAPABILITY_PRODUCT_CONTRACT_ID,
      actual: closure.contract_id,
    });
  }
  if (closure.fail_closed !== true) {
    throw new ContractViolationError('capability product closure must declare fail_closed=true');
  }

  const sourceIds = new Set(requireArray(closure.source_ids, 'source_ids').map((item) => requireNonEmptyString(item, 'source_id')));
  const missingSourceIds = CAPABILITY_PRODUCT_SOURCE_IDS.filter((sourceId) => !sourceIds.has(sourceId));
  if (missingSourceIds.length > 0) {
    throw new ContractViolationError('missing source ids from capability product closure', { missingSourceIds });
  }

  const surfaceRows = requireArray(closure.surfaces, 'surfaces').map((row, index) => {
    const surface = requireObject(row, `surfaces[${index}]`);
    const sourceId = requireNonEmptyString(surface.source_id, `surfaces[${index}].source_id`);
    const status = requireNonEmptyString(surface.status, `surfaces[${index}].status`);
    if (!TERMINAL_CLOSURE_STATUSES.has(status)) {
      throw new ContractViolationError('surface row is not terminal', { sourceId, status });
    }
    const evidence = requireArray(surface.evidence, `surfaces[${index}].evidence`);
    if (evidence.length === 0) {
      throw new ContractViolationError('surface row must cite evidence', { sourceId });
    }
    return Object.freeze({
      sourceId,
      path: requireNonEmptyString(surface.path, `surfaces[${index}].path`),
      status,
      evidence: Object.freeze(evidence.map((item) => requireNonEmptyString(item, `surfaces[${index}].evidence[]`))),
    });
  });
  const surfaceIds = new Set(surfaceRows.map((row) => row.sourceId));
  const missingSurfaceRows = CAPABILITY_PRODUCT_SOURCE_IDS.filter((sourceId) => !surfaceIds.has(sourceId));
  if (missingSurfaceRows.length > 0) {
    throw new ContractViolationError('missing surface rows from capability product closure', { missingSurfaceRows });
  }

  const verification = requireObject(closure.verification, 'verification');
  if (verification.passed !== true) {
    throw new ContractViolationError('verification must pass before closure is terminal');
  }
  const command = requireNonEmptyString(verification.command, 'verification.command');
  if (!command.includes('tests/test_rcg_0021_p01.py')) {
    throw new ContractViolationError('verification command does not exercise RCG-0021-P01 tests', { command });
  }

  return Object.freeze({
    contractId: closure.contract_id,
    sourceIds: Object.freeze([...sourceIds]),
    surfaces: Object.freeze(surfaceRows),
    verification: Object.freeze({ command, passed: true }),
  });
}
