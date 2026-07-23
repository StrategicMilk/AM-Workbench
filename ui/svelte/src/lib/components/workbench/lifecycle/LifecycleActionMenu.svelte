<script>
  import { LIFECYCLE_ACTIONS } from './launcher_store.svelte.js';

  let { onAction = () => {}, disabled = false } = $props();
  let actions = $derived(LIFECYCLE_ACTIONS);
</script>

<section class="action-menu" data-testid="launcher-action-menu" aria-label="Launcher actions">
  {#each actions as action}
    <button type="button" data-action-id={action} data-testid={`launcher-action-${action}`} {disabled} onclick={() => onAction(action)}>
      {action.replaceAll('_', ' ')}
    </button>
  {/each}
</section>

<style>
  .action-menu {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: 8px;
  }

  button {
    min-height: 34px;
    border: 1px solid var(--border-default, #334155);
    border-radius: 6px;
    background: var(--surface-elevated, #111827);
    color: var(--text-primary, #e5e7eb);
    cursor: pointer;
    text-transform: capitalize;
  }

  button:disabled {
    cursor: not-allowed;
    opacity: 0.55;
  }
</style>
