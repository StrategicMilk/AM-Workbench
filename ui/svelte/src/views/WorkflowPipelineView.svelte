<script>
  import WorkflowBuilder from '$components/workbench/workflow-builder/WorkflowBuilder.svelte';

  /** @type {{ onNavigate?: (view: string) => void }} */
  let { onNavigate = null } = $props();

  /** @type {string} */
  let lastSavedId = $state('');

  /**
   * @param {string} pipelineId
   */
  function handleSaved(pipelineId) {
    lastSavedId = pipelineId;
  }
</script>

<section class="workflow-pipeline-view" aria-label="Workflow pipeline builder view">
  <header class="view-header">
    <h1>Workflow Pipeline Builder</h1>
    <p class="view-subtitle">
      Compose, save, load, and validate reusable agent pipelines.
      {#if lastSavedId}
        <span class="last-saved">Last saved: <code>{lastSavedId}</code></span>
      {/if}
    </p>
  </header>

  <div class="view-body">
    <WorkflowBuilder onSaved={handleSaved} />
  </div>
</section>

<style>
  .workflow-pipeline-view {
    display: flex;
    flex-direction: column;
    height: 100%;
    padding: 0;
  }

  .view-header {
    padding: 0.75rem 1rem 0.5rem;
    border-bottom: 1px solid var(--color-border, #e5e7eb);
    flex-shrink: 0;
  }

  .view-header h1 {
    font-size: 1.1rem;
    font-weight: 600;
    margin: 0 0 0.2rem;
  }

  .view-subtitle {
    font-size: 0.8125rem;
    color: var(--color-muted, #6b7280);
    margin: 0;
  }

  .last-saved {
    margin-left: 0.5rem;
    color: var(--color-success, #059669);
  }

  .view-body {
    flex: 1;
    overflow: hidden;
    display: flex;
    flex-direction: column;
  }
</style>
