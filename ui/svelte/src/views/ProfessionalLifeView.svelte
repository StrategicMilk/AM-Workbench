<script>
  import { SensitiveWorkflowPanel } from '$lib/components/workbench/life_admin';
  import { ProfessionalDraftPanel } from '$lib/components/workbench/professional';

  const SURFACE_KEY = 'workbench.professionalLifeSurface';

  let { projectId = 'default' } = $props();
  let activeSurface = $state('workflow');

  $effect(() => {
    try {
      const requestedSurface = window.sessionStorage.getItem(SURFACE_KEY);
      if (requestedSurface === 'draft' || requestedSurface === 'workflow') {
        activeSurface = requestedSurface;
        window.sessionStorage.removeItem(SURFACE_KEY);
      }
    } catch {
      // Session storage is optional; sidebar/default routing still works.
    }
  });
</script>

<section class="professional-life-view" aria-label="Professional life workspace" data-project-id={projectId}>
  <header class="professional-life-header">
    <div>
      <h1>Professional Life</h1>
      <p>{projectId}</p>
    </div>
    <div class="surface-tabs" role="tablist" aria-label="Professional life surfaces">
      <button
        type="button"
        role="tab"
        aria-selected={activeSurface === 'workflow'}
        class:active={activeSurface === 'workflow'}
        onclick={() => {
          activeSurface = 'workflow';
        }}
      >
        Workflow
      </button>
      <button
        type="button"
        role="tab"
        aria-selected={activeSurface === 'draft'}
        class:active={activeSurface === 'draft'}
        onclick={() => {
          activeSurface = 'draft';
        }}
      >
        Draft
      </button>
    </div>
  </header>

  {#if activeSurface === 'workflow'}
    <SensitiveWorkflowPanel {projectId} />
  {:else}
    <ProfessionalDraftPanel {projectId} />
  {/if}
</section>

<style>
  .professional-life-view {
    display: grid;
    gap: 16px;
    width: 100%;
    color: var(--text-primary, #111827);
  }

  .professional-life-header {
    display: flex;
    justify-content: space-between;
    align-items: flex-end;
    gap: 16px;
    padding: 18px 18px 0;
  }

  h1,
  p {
    margin: 0;
  }

  h1 {
    font-size: 1.35rem;
    font-weight: 650;
  }

  p {
    color: var(--text-secondary, #4b5563);
    font-size: 0.88rem;
  }

  .surface-tabs {
    display: inline-flex;
    gap: 2px;
    padding: 3px;
    border: 1px solid var(--border-color, #d1d5db);
    border-radius: 8px;
    background: var(--surface-secondary, #f8fafc);
  }

  .surface-tabs button {
    min-width: 92px;
    min-height: 32px;
    border: 0;
    border-radius: 6px;
    background: transparent;
    color: inherit;
    cursor: pointer;
    font: inherit;
  }

  .surface-tabs button.active,
  .surface-tabs button:hover {
    background: var(--surface-primary, #fff);
  }

  @media (max-width: 720px) {
    .professional-life-header {
      align-items: stretch;
      display: grid;
    }

    .surface-tabs {
      width: 100%;
    }

    .surface-tabs button {
      flex: 1;
    }
  }
</style>
