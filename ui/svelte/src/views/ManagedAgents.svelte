<script>
  import { appState } from '$lib/stores/app.svelte.js';
  import { ManagedAgentsPanel } from '$lib/components/workbench/managed-agents';

  let projectId = $derived(appState.currentProjectId || 'default');
</script>

<section class="managed-agents-view">
  <ManagedAgentsPanel {projectId} />
</section>

<style>
  .managed-agents-view {
    min-width: 0;
  }

  @media (max-width: 800px) {
    .managed-agents-view {
      overflow-x: hidden;
      padding: 0 12px;
    }
  }
</style>
