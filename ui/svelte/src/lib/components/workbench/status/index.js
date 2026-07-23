export { default as FixActionDrawer } from './FixActionDrawer.svelte';
export { default as HealthResultTable } from './HealthResultTable.svelte';
export { default as SettingsActionReceiptPanel } from './SettingsActionReceiptPanel.svelte';
export { default as StatusSummaryPanel } from './StatusSummaryPanel.svelte';
export { createWorkbenchStatusStore } from './status_store.svelte.js';

export const WORKBENCH_STATUS_REQUIRED_FIELDS = Object.freeze(['state_counts', 'results']);

export function normalizeWorkbenchStatusSnapshot(snapshot = {}) {
  if (snapshot === null || typeof snapshot !== 'object' || Array.isArray(snapshot)) {
    throw new TypeError('Workbench status snapshot must be an object');
  }
  const stateCounts = snapshot.state_counts ?? snapshot.status_counts;
  const results = snapshot.results ?? snapshot.health_results;
  if (stateCounts === null || typeof stateCounts !== 'object' || Array.isArray(stateCounts)) {
    throw new TypeError('Workbench status snapshot is missing state_counts');
  }
  if (!Array.isArray(results)) {
    throw new TypeError('Workbench status snapshot is missing results array');
  }
  return Object.freeze({
    state_counts: Object.freeze({ ...stateCounts }),
    results: Object.freeze([...results]),
  });
}
