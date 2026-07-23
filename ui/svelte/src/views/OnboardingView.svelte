<script>
  import { appState } from '$lib/stores/app.svelte.js';
  import { COMPLETED_SETUP_VIEW } from '../routes/defaultLanding.js';

  const setupRows = [
    { icon: 'fas fa-microchip', label: 'Local models', view: 'models' },
    { icon: 'fas fa-folder-tree', label: 'Project workspace', view: 'workflow' },
    { icon: 'fas fa-heart-pulse', label: 'Workbench status', view: 'workbench-status' },
  ];

  function completeOnboarding(view = COMPLETED_SETUP_VIEW) {
    appState.setupComplete = true;
    appState.currentView = view;
  }
</script>

<section class="onboarding-view" aria-label="First-run onboarding">
  <div class="onboarding-header">
    <h1>Vetinari Workbench</h1>
    <div class="onboarding-actions">
      <button type="button" class="primary" onclick={() => completeOnboarding()}>
        <i class="fas fa-check" aria-hidden="true"></i>
        Continue
      </button>
    </div>
  </div>

  <div class="onboarding-grid">
    {#each setupRows as row}
      <button
        type="button"
        class="setup-row"
        aria-label={`Open ${row.label} setup`}
        onclick={() => completeOnboarding(row.view)}
      >
        <i class={row.icon} aria-hidden="true"></i>
        <span>{row.label}</span>
        <i class="fas fa-arrow-right" aria-hidden="true"></i>
      </button>
    {/each}
  </div>
</section>

<style>
  .onboarding-view {
    min-height: 100%;
    display: grid;
    gap: 18px;
    align-content: start;
    padding: 28px;
    color: var(--text-primary);
  }

  .onboarding-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    flex-wrap: wrap;
  }

  h1 {
    margin: 0;
    font-size: 1.6rem;
    letter-spacing: 0;
  }

  .onboarding-actions,
  .setup-row {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  button {
    border: 1px solid var(--border-default);
    border-radius: 8px;
    background: var(--surface-elevated);
    color: var(--text-primary);
    font: inherit;
    min-height: 38px;
    cursor: pointer;
  }

  button:focus-visible {
    outline: 2px solid var(--primary);
    outline-offset: 2px;
  }

  .primary {
    background: var(--primary);
    color: var(--text-on-primary);
    border-color: var(--primary);
    padding: 8px 12px;
  }

  .onboarding-grid {
    display: grid;
    grid-template-columns: repeat(3, minmax(180px, 1fr));
    gap: 10px;
  }

  .setup-row {
    justify-content: space-between;
    padding: 14px;
    text-align: left;
  }

  .setup-row span {
    flex: 1;
    min-width: 0;
    overflow-wrap: anywhere;
  }

  @media (max-width: 760px) {
    .onboarding-view {
      padding: 18px;
    }

    .onboarding-grid {
      grid-template-columns: 1fr;
    }
  }
</style>
