<script>
  // Props use $props(); do not replace with legacy export let in Svelte 5.
  let { proposalId, gatePassed, disabled = false, onDecision = null } = $props();

  let decidedBy = $state('');
  let rationale = $state('');
  let inFlight = $state(false);
  let error = $state('');

  function submit(accepted) {
    error = '';
    if (!decidedBy.trim()) {
      error = 'Please enter your name.';
      return;
    }
    if (!accepted && !rationale.trim()) {
      error = 'Reject requires a rationale.';
      return;
    }
    if (accepted && !gatePassed) {
      return;
    }
    inFlight = true;
    onDecision?.({
      proposal_id: proposalId,
      accepted,
      decided_by: decidedBy.trim(),
      rationale: rationale.trim(),
    });
    inFlight = false;
  }
</script>

<div class="promotion-decision-form" aria-label="Promotion decision form">
  <label>
    Decided by
    <input bind:value={decidedBy} disabled={disabled || inFlight} autocomplete="name" />
  </label>
  <label>
    Rationale
    <textarea bind:value={rationale} disabled={disabled || inFlight} rows="3"></textarea>
  </label>
  {#if error}
    <p class="form-error" role="alert" aria-live="assertive">{error}</p>
  {/if}
  <div class="actions">
    <button
      type="button"
      class="approve"
      disabled={disabled || inFlight || !gatePassed}
      title={gatePassed ? 'Approve promotion' : 'gate is blocked; only Reject is available'}
      onclick={() => submit(true)}
    >
      Approve
    </button>
    <button type="button" class="reject" disabled={disabled || inFlight} onclick={() => submit(false)}>
      Reject
    </button>
  </div>
</div>

<style>
  .promotion-decision-form {
    display: grid;
    gap: 0.65rem;
  }

  label {
    display: grid;
    font-size: 0.82rem;
    font-weight: 700;
    gap: 0.3rem;
  }

  input,
  textarea {
    border: 1px solid var(--border-default, #b9c0c9);
    border-radius: 6px;
    font: inherit;
    padding: 0.5rem;
    resize: vertical;
  }

  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
  }

  button {
    border: 0;
    border-radius: 6px;
    cursor: pointer;
    font: inherit;
    font-weight: 700;
    min-height: 44px;
    padding: 0.5rem 0.8rem;
  }

  button:disabled {
    cursor: not-allowed;
    opacity: 0.55;
  }

  .approve {
    background: var(--success, #256f46);
    color: var(--surface-primary, #fff);
  }

  .reject {
    background: var(--danger, #772f2a);
    color: var(--surface-primary, #fff);
  }

  .form-error {
    color: var(--danger, #9a2d22);
    font-size: 0.85rem;
    font-weight: 700;
    margin: 0;
  }
</style>
