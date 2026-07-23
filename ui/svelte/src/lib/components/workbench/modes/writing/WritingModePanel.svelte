<script>
  const {
    audience = 'general',
    draftBranch = 'draft',
    verifiedFacts = 0,
    onModeChange = () => {},
  } = $props();

  const audiences = ['general', 'technical', 'executive', 'reviewer'];
  const branches = ['draft', 'revision', 'fact_check', 'publish_ready'];

  let selectedAudience = $state(audiences.includes(audience) ? audience : 'general');
  let selectedBranch = $state(branches.includes(draftBranch) ? draftBranch : 'draft');
  let factCount = $state(Number.isFinite(Number(verifiedFacts)) ? Number(verifiedFacts) : 0);
  let validationError = $state('');

  function emitWritingMode() {
    if (!Number.isFinite(Number(factCount)) || Number(factCount) < 0) {
      validationError = 'Verified facts must be a non-negative number.';
      return;
    }
    validationError = '';
    onModeChange({
      audience: selectedAudience,
      draftBranch: selectedBranch,
      verifiedFacts: Number(factCount),
    });
  }

  $effect(() => {
    selectedAudience;
    selectedBranch;
    factCount;
    emitWritingMode();
  });
</script>

<section class="writing-mode-panel" aria-label="Writing mode">
  <div class="mode-title">
    <i class="fas fa-pen-nib"></i>
    <span>Writing</span>
  </div>
  <div class="writing-controls">
    <label>
      <span>Audience</span>
      <select bind:value={selectedAudience}>
        {#each audiences as option}
          <option value={option}>{option}</option>
        {/each}
      </select>
    </label>
    <label>
      <span>Branch</span>
      <select bind:value={selectedBranch}>
        {#each branches as option}
          <option value={option}>{option}</option>
        {/each}
      </select>
    </label>
    <label>
      <span>Facts</span>
      <input bind:value={factCount} min="0" type="number" />
    </label>
  </div>
  {#if validationError}
    <p class="mode-error" role="alert">{validationError}</p>
  {/if}
</section>

<style>
  .writing-mode-panel {
    display: grid;
    gap: 8px;
    padding: 10px;
    border: 1px solid var(--border-default);
    border-radius: 6px;
    background: var(--surface-primary);
  }

  .mode-title {
    display: flex;
    align-items: center;
    gap: 8px;
    color: var(--text-primary);
    font-size: 0.8125rem;
    font-weight: 700;
  }

  .writing-controls {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 6px;
  }

  label {
    display: grid;
    gap: 4px;
    min-width: 0;
  }

  span {
    color: var(--text-muted);
    font-size: 0.6875rem;
  }

  select,
  input {
    min-height: 32px;
    border: 1px solid var(--border-default);
    border-radius: 6px;
    background: var(--surface-elevated);
    color: var(--text-primary);
    padding: 6px 8px;
    min-width: 0;
  }

  .mode-error {
    margin: 0;
    color: var(--danger);
    font-size: 0.76rem;
  }

  @media (max-width: 640px) {
    .writing-controls {
      grid-template-columns: 1fr;
    }
  }
</style>
