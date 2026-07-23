export { default as WorkflowBuilderCanvas } from './WorkflowBuilderCanvas.svelte';
export { default as WorkflowPreviewPanel } from './WorkflowPreviewPanel.svelte';
export { default as WorkflowRuntimeConsole } from './WorkflowRuntimeConsole.svelte';
export { createWorkflowBuilderStore } from './workflow_builder_store.svelte.js';

export const RCG_0021_P05_WORKFLOW_RECEIPT = 'rcg-0021-p05:workflow-builder:fail-closed';

const DEFAULT_RUNTIME_SETTINGS = Object.freeze({
  max_parallel_steps: 2,
  safety_mode: 'simulation_only',
  channel_preview_only: true,
});

export function normalizeWorkflowGraph(graph = {}) {
  if (!graph || typeof graph !== 'object' || Array.isArray(graph)) {
    return {
      ok: false,
      issue: 'workflow graph payload must be an object',
      graph: { steps: [], edges: [], safety_mode: 'blocked' },
      evidence_ref: RCG_0021_P05_WORKFLOW_RECEIPT,
    };
  }

  if (!Array.isArray(graph.steps)) {
    return {
      ok: false,
      issue: 'workflow graph missing steps array',
      graph: { ...graph, steps: [], edges: [], safety_mode: graph.safety_mode ?? 'blocked' },
      evidence_ref: RCG_0021_P05_WORKFLOW_RECEIPT,
    };
  }

  const steps = graph.steps.filter((step) => (
    step
    && typeof step === 'object'
    && typeof step.step_id === 'string'
    && step.step_id.trim()
    && typeof step.label === 'string'
    && step.label.trim()
  ));
  if (steps.length !== graph.steps.length) {
    return {
      ok: false,
      issue: 'workflow graph contains invalid step records',
      graph: { ...graph, steps, edges: [], safety_mode: graph.safety_mode ?? 'blocked' },
      evidence_ref: RCG_0021_P05_WORKFLOW_RECEIPT,
    };
  }

  const edges = Array.isArray(graph.edges) ? graph.edges : [];
  return {
    ok: true,
    issue: '',
    graph: { ...graph, steps, edges, safety_mode: graph.safety_mode ?? 'simulation_only' },
    evidence_ref: RCG_0021_P05_WORKFLOW_RECEIPT,
  };
}

export function normalizeWorkflowRuntimeSnapshot(snapshot = {}) {
  if (!snapshot || typeof snapshot !== 'object' || Array.isArray(snapshot)) {
    return {
      ok: false,
      issue: 'workflow runtime snapshot must be an object',
      snapshot: { runtime_settings: { ...DEFAULT_RUNTIME_SETTINGS }, saved_graph_count: 0 },
      evidence_ref: RCG_0021_P05_WORKFLOW_RECEIPT,
    };
  }

  const settings = snapshot.runtime_settings;
  if (!settings || typeof settings !== 'object' || Array.isArray(settings)) {
    return {
      ok: false,
      issue: 'workflow runtime snapshot missing runtime_settings object',
      snapshot: { ...snapshot, runtime_settings: { ...DEFAULT_RUNTIME_SETTINGS } },
      evidence_ref: RCG_0021_P05_WORKFLOW_RECEIPT,
    };
  }

  const maxParallel = Number(settings.max_parallel_steps);
  if (!Number.isInteger(maxParallel) || maxParallel < 1) {
    return {
      ok: false,
      issue: 'workflow runtime max_parallel_steps must be a positive integer',
      snapshot: { ...snapshot, runtime_settings: { ...DEFAULT_RUNTIME_SETTINGS } },
      evidence_ref: RCG_0021_P05_WORKFLOW_RECEIPT,
    };
  }

  return {
    ok: true,
    issue: '',
    snapshot: {
      ...snapshot,
      runtime_settings: {
        ...DEFAULT_RUNTIME_SETTINGS,
        ...settings,
        max_parallel_steps: maxParallel,
      },
    },
    evidence_ref: RCG_0021_P05_WORKFLOW_RECEIPT,
  };
}

export function workflowReadinessEvidence(status) {
  return {
    receipt_id: RCG_0021_P05_WORKFLOW_RECEIPT,
    ok: Boolean(status?.ok),
    issue: status?.issue || '',
  };
}
