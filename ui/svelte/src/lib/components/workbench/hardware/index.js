export { default as HardwareBenchmarkMatrix } from './HardwareBenchmarkMatrix.svelte';
export { default as HardwareOptimizationProposalList } from './HardwareOptimizationProposalList.svelte';
export { default as HardwareTwinPanel } from './HardwareTwinPanel.svelte';

export function normalizeHardwareSnapshot(snapshot = {}) {
  return {
    ...snapshot,
    observations: Array.isArray(snapshot.observations) ? snapshot.observations : [],
    evidence_ids: Array.isArray(snapshot.evidence_ids) ? snapshot.evidence_ids : [],
  };
}
