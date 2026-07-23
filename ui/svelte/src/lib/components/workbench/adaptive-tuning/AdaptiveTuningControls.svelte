<script>
  import { ADAPTIVE_TUNING_STATE } from '$lib/uiEnums.js';

  let { hypothesisId = '', state = ADAPTIVE_TUNING_STATE.PENDING, onAction = () => {} } = $props();

  function actions() {
    return [
      {
        id: 'allow',
        label: 'Allow',
        disabled: [ADAPTIVE_TUNING_STATE.ACTIVE, ADAPTIVE_TUNING_STATE.BLOCKED].includes(state),
      },
      { id: 'reject', label: 'Reject', disabled: state === ADAPTIVE_TUNING_STATE.REJECTED },
      {
        id: 'edit',
        label: 'Edit',
        disabled: [ADAPTIVE_TUNING_STATE.FORGOTTEN, ADAPTIVE_TUNING_STATE.REVOKED].includes(state),
      },
      { id: 'forget', label: 'Forget', disabled: state === ADAPTIVE_TUNING_STATE.FORGOTTEN },
      { id: 'revoke', label: 'Revoke', disabled: state === ADAPTIVE_TUNING_STATE.REVOKED },
      { id: 'preview', label: 'Preview', disabled: false },
      { id: 'rollback', label: 'Rollback', disabled: state !== 'active' },
      { id: 'policy_override', label: 'Policy', disabled: state === 'active' },
    ];
  }

  function emitAction(action) {
    onAction({ hypothesisId, action });
  }
</script>

<div class="controls" aria-label="Adaptive tuning controls">
  {#each actions() as action}
    <button
      type="button"
      disabled={action.disabled}
      aria-label={`${action.label} adaptive tuning ${hypothesisId || 'hypothesis'}`}
      onclick={() => emitAction(action.id)}
    >
      {action.label}
    </button>
  {/each}
</div>

<style>
  .controls {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }

  button {
    min-height: 34px;
    border: 1px solid var(--border-default, #334155);
    border-radius: 8px;
    background: var(--surface-elevated, #111827);
    color: var(--text-primary, #e5e7eb);
    padding: 7px 10px;
    font: inherit;
    font-size: 12px;
  }

  button:disabled {
    opacity: 0.45;
  }
</style>
