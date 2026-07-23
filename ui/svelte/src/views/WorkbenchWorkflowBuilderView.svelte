<script>
  import {
    WorkflowBuilderCanvas,
    WorkflowPreviewPanel,
    WorkflowRuntimeConsole,
    createWorkflowBuilderStore,
  } from '$lib/components/workbench/workflow_builder';
  import HelpPopover from '$lib/components/help/HelpPopover.svelte';
  import HelpTooltip from '$lib/components/help/HelpTooltip.svelte';

  let { projectId = 'default' } = $props();
  const store = createWorkflowBuilderStore(() => projectId || 'default');
  let state = $derived(store.state);

  $effect(() => {
    store.load().catch((error) => {
      state.error = error.message;
    });
  });
</script>

<section class="workflow-builder" aria-label="Workflow builder">
  <header>
    <div>
      <h1>Workflow Builder</h1>
      <p>Design, preview, and save automated agent workflows for project {projectId}.</p>
      <HelpPopover
        title="Workflow builder"
        body="Use the canvas to compose agent workflows from step nodes. Simple mode chains steps linearly; advanced mode allows branching and parallel concurrency groups. Step types: task (run an agent task), gate (conditional branch on signal), wait (defer until external event), notify (emit a channel delivery). Concurrency group limit: steps sharing a group run in parallel up to the declared limit; exceeding it queues overflow steps. Validate before saving to catch missing edge connections and unsatisfied gate conditions. Preview runs a dry simulation without committing the workflow."
        severity="info"
      />
    </div>
    <div class="actions">
      <button onclick={() => store.validate()}>Validate</button>
      <button onclick={() => store.preview()}>Preview</button>
      <button onclick={() => store.save()} disabled={state.saving}>Save</button>
    </div>
  </header>

  {#if state.error}
    <p class="error" role="alert" aria-live="assertive">{state.error}</p>
  {:else if state.saving}
    <p class="status" role="status" aria-live="polite">Saving workflow.</p>
  {/if}

  <div class="layout">
    <WorkflowBuilderCanvas graph={state.graph} />
    <div class="side">
      <WorkflowPreviewPanel preview={state.preview} validation={state.validation} />
      <WorkflowRuntimeConsole snapshot={state.console} onSettings={(settings) => store.updateSettings(settings)} />
    </div>
  </div>
</section>

<style>
  .workflow-builder {
    display: grid;
    gap: 18px;
    color: var(--text-default, #e5e7eb);
  }

  header,
  .actions {
    display: flex;
    gap: 10px;
  }

  header {
    align-items: flex-start;
    justify-content: space-between;
  }

  h1,
  p {
    margin: 0;
    letter-spacing: 0;
  }

  h1 {
    font-size: 24px;
  }

  p {
    color: var(--text-muted, #94a3b8);
    font-size: 13px;
  }

  button {
    border: 1px solid var(--border-default, #334155);
    border-radius: 8px;
    background: var(--surface-elevated, #111827);
    color: inherit;
    padding: 8px 10px;
  }

  .layout {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(280px, 360px);
    gap: 16px;
  }

  .side {
    display: grid;
    gap: 12px;
  }

  .error {
    color: #fca5a5;
  }

  .status {
    color: var(--text-muted, #94a3b8);
  }

  @media (max-width: 860px) {
    header {
      display: grid;
    }

    .layout {
      grid-template-columns: 1fr;
    }
  }
</style>
