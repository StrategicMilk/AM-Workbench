import {
  getWorkflowBuilderGraph,
  getWorkflowBuilderConsole,
  getWorkflowBuilderMetadata,
  listWorkflowBuilderGraphs,
  previewWorkflowBuilderGraph,
  saveWorkflowBuilderGraph,
  updateWorkflowBuilderSettings,
  validateWorkflowBuilderGraph,
} from '$lib/api.js';
import { GraphState } from '$lib/contracts';

export const SAFETY_MODES = Object.freeze({
  SIMULATION_ONLY: 'simulation_only',
  LIVE: 'live',
  REVIEW: 'review',
});

export function createWorkflowBuilderStore(projectId = 'default') {
  const currentProjectId = () => (typeof projectId === 'function' ? projectId() : projectId || 'default');
  const state = $state({
    metadata: null,
    graph: sampleGraph(),
    validation: null,
    preview: null,
    console: null,
    saving: false,
    error: '',
  });

  return {
    get state() {
      return state;
    },
    async load() {
      state.metadata = await getWorkflowBuilderMetadata();
      state.console = await getWorkflowBuilderConsole(currentProjectId());
      const graphs = await listWorkflowBuilderGraphs(currentProjectId());
      if (graphs.state === GraphState.READY && graphs.graphs?.length) {
        const activeGraphId = state.console?.active_graph_id ?? graphs.graphs.at(-1)?.graph_id;
        const selected = graphs.graphs.find((graph) => graph.graph_id === activeGraphId) ?? graphs.graphs.at(-1);
        if (selected?.graph_id) {
          const payload = await getWorkflowBuilderGraph(currentProjectId(), selected.graph_id);
          state.graph = payload.graph;
        }
      }
    },
    async validate() {
      state.validation = await validateWorkflowBuilderGraph(state.graph);
      return state.validation;
    },
    async preview() {
      const payload = await previewWorkflowBuilderGraph(state.graph);
      state.validation = payload.validation;
      state.preview = payload.preview;
      return payload;
    },
    async save() {
      state.saving = true;
      try {
        const payload = await saveWorkflowBuilderGraph(currentProjectId(), state.graph);
        state.console = await getWorkflowBuilderConsole(currentProjectId());
        return payload;
      } finally {
        state.saving = false;
      }
    },
    async updateSettings(settings) {
      state.console = {
        ...(state.console ?? {}),
        runtime_settings: await updateWorkflowBuilderSettings(currentProjectId(), settings),
      };
    },
  };
}

function sampleGraph() {
  return {
    schema_version: 1,
    graph_id: 'draft',
    name: 'Draft Workflow',
    safety_mode: SAFETY_MODES.SIMULATION_ONLY,
    steps: [
      { step_id: 'draft', kind: 'prompt', label: 'Draft request', config: {} },
      { step_id: 'approval', kind: 'approval', label: 'Approval check', config: { approval_policy: 'default' } },
      { step_id: 'deliver', kind: 'channel_delivery', label: 'Delivery preview', config: { channel_id: 'desktop', preview_only: true } },
    ],
    edges: [
      { source: 'draft', target: 'approval' },
      { source: 'approval', target: 'deliver' },
    ],
    metadata: { persistent_threads: false },
  };
}
