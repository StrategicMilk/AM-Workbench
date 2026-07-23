<script>
  let {
    canonBranches = [],
    exploratoryBranches = [],
    activeBranch = 'canon-main',
    rejection = null,
    onBranchChange = () => {},
  } = $props();

  let selectedBranch = $state('canon-main');
  let selectedKind = $derived(
    exploratoryBranches.some((branch) => branch.id === selectedBranch) ? 'exploratory' : 'canon'
  );
  let rejectionMessage = $derived(selectedKind === 'exploratory' ? rejection : null);

  $effect(() => {
    if (activeBranch !== selectedBranch) {
      selectedBranch = activeBranch;
    }
  });

  function selectBranch(branchId) {
    selectedBranch = branchId;
    onBranchChange(branchId);
  }
</script>

<section class="branch-switcher" data-testid="creative-branch-switcher">
  <div class="branch-column">
    <h3>Canon</h3>
    {#each canonBranches as branch}
      <button
        type="button"
        class:active={selectedBranch === branch.id}
        aria-pressed={selectedBranch === branch.id}
        onclick={() => selectBranch(branch.id)}
      >
        <span>{branch.label}</span>
        <small>{branch.worldId}</small>
      </button>
    {:else}
      <p class="empty" role="status">No canon branches available.</p>
    {/each}
  </div>

  <div class="branch-column">
    <h3>Exploratory</h3>
    {#each exploratoryBranches as branch}
      <button
        type="button"
        class:active={selectedBranch === branch.id}
        aria-pressed={selectedBranch === branch.id}
        onclick={() => selectBranch(branch.id)}
      >
        <span>{branch.label}</span>
        <small>{branch.worldId}</small>
      </button>
    {:else}
      <p class="empty" role="status">No exploratory branches available.</p>
    {/each}
  </div>

  {#if rejectionMessage}
    <div class="rejection" role="status" aria-live="polite">
      <strong>{rejectionMessage.type}</strong>
      <span>{rejectionMessage.blockers.join(', ')}</span>
    </div>
  {/if}
</section>

<style>
  .branch-switcher {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 12px;
  }

  .branch-column {
    display: grid;
    gap: 8px;
  }

  h3 {
    margin: 0;
    font-size: 0.9rem;
  }

  button {
    display: grid;
    gap: 3px;
    min-height: 54px;
    border: 1px solid var(--border-color, #d1d5db);
    border-radius: 8px;
    padding: 9px;
    background: var(--surface-primary, #fff);
    color: inherit;
    font: inherit;
    text-align: left;
    cursor: pointer;
  }

  button.active {
    border-color: var(--accent-color, #2563eb);
    background: var(--accent-subtle, #eff6ff);
  }

  small,
  .rejection span {
    color: var(--text-secondary, #64748b);
  }

  .rejection,
  .empty {
    grid-column: 1 / -1;
    display: grid;
    gap: 4px;
    border: 1px solid #f59e0b;
    border-radius: 8px;
    padding: 10px;
    background: #fffbeb;
    color: #78350f;
  }

  .empty {
    color: var(--text-secondary, #64748b);
    background: var(--surface-secondary, #f8fafc);
    border-color: var(--border-color, #d1d5db);
  }

  @media (max-width: 720px) {
    .branch-switcher {
      grid-template-columns: 1fr;
    }
  }
</style>
