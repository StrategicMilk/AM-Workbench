function requirePayload(data, message) {
  if (data === null || data === undefined) {
    throw new TypeError(message);
  }
}

/**
 * FSA-0343 - normalize gateway policy decision payloads.
 * @param {any} data
 * @returns {Array<any>}
 */
export function unwrapDecisions(data) {
  requirePayload(data, 'Gateway decisions payload missing decisions/rows key');
  if (Array.isArray(data.decisions)) {
    return data.decisions;
  }
  if (Array.isArray(data.rows)) {
    return data.rows;
  }
  if (Array.isArray(data)) {
    return data;
  }
  return [];
}

/**
 * FSA-0348 - normalize memory entries and sessions payloads.
 * @param {any} data
 * @returns {Array<any>}
 */
export function unwrapMemoryItems(data) {
  requirePayload(data, 'Memory payload missing items key');
  if (Array.isArray(data.items)) {
    return data.items;
  }
  if (Array.isArray(data)) {
    return data;
  }
  return [];
}

/**
 * FSA-0349 - normalize model listing payloads.
 * @param {any} data
 * @returns {Array<any>}
 */
export function unwrapModels(data) {
  requirePayload(data, 'Models payload missing models/data key');
  if (Array.isArray(data.models)) {
    return data.models;
  }
  if (Array.isArray(data.data)) {
    return data.data;
  }
  if (Array.isArray(data)) {
    return data;
  }
  return [];
}

/**
 * FSA-0153 - normalize capability record payloads.
 * @param {any} data
 * @returns {Array<any>}
 */
export function unwrapCapabilityRecords(data) {
  requirePayload(data, 'Capability payload missing records/capabilities key');
  if (Array.isArray(data.records)) {
    return data.records;
  }
  if (Array.isArray(data.capabilities)) {
    return data.capabilities;
  }
  if (Array.isArray(data)) {
    return data;
  }
  return [];
}

/**
 * FSA-0347/FSA-1528 - normalize managed-agent snapshots.
 * @param {any} data
 * @returns {{agents: Array<any>, dependency_contracts: Array<any>, status: string, degradation_reasons: Array<any>, user_intervention: any}}
 */
export function unwrapManagedAgentsSnapshot(data) {
  requirePayload(data, 'Managed agents snapshot payload is missing');
  return {
    agents: Array.isArray(data.agents) ? data.agents : [],
    dependency_contracts: Array.isArray(data.dependency_contracts)
      ? data.dependency_contracts
      : [],
    status: data.status ?? 'unknown',
    degradation_reasons: Array.isArray(data.degradation_reasons)
      ? data.degradation_reasons
      : [],
    user_intervention: data.user_intervention ?? { attention_agent_ids: [] },
  };
}

/**
 * FSA-0346/FSA-1534 - normalize readiness wrapper payloads.
 * @param {any} data
 * @returns {any}
 */
export function unwrapReadiness(data) {
  requirePayload(data, 'Readiness payload is missing');
  return data.readiness ?? data;
}

/**
 * FSA-0157 - normalize project creation responses.
 * @param {any} data
 * @returns {any}
 */
export function unwrapProjectResponse(data) {
  requirePayload(data, 'Project response payload is missing');
  return {
    ...data,
    project_id: data.project_id ?? data.id ?? null,
  };
}
