<script>
  let { commands = [], onRun = () => {} } = $props();
</script>

<section class="command-surface" aria-label="Workbench commands" data-testid="workbench-command-surface">
  <div class="command-header">
    <h2>Commands</h2>
  </div>

  <div class="command-list">
    {#each commands as command (command.command_id)}
      <button
        type="button"
        class:blocked={!command.enabled}
        disabled={!command.enabled}
        title={command.blocked_reason || command.why}
        aria-label={`${command.label}: ${command.requires_approval ? 'requires approval' : 'safe'}${command.enabled ? '' : `, blocked: ${command.blocked_reason || command.why || 'unavailable'}`}`}
        data-testid={`workbench-command-${command.command_id}`}
        onclick={() => onRun(command)}
      >
        <span class="command-label">{command.label}</span>
        <span class="command-meta">
          {#if command.requires_approval}
            approval
          {:else}
            safe
          {/if}
        </span>
      </button>
    {/each}
  </div>
</section>

<style>
  .command-surface {
    border: 1px solid var(--border-default, #334155);
    border-radius: 8px;
    background: var(--surface-elevated, #111827);
    min-width: 0;
  }

  .command-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    border-bottom: 1px solid var(--border-default, #334155);
    padding: 10px 12px;
  }

  h2 {
    margin: 0;
    font-size: 0.92rem;
  }

  .command-meta {
    color: var(--text-muted, #94a3b8);
    font-size: 0.78rem;
  }

  .command-list {
    display: grid;
    gap: 4px;
    padding: 8px;
  }

  button {
    border: 0;
    border-radius: 6px;
    background: transparent;
    color: var(--text-primary, #e5e7eb);
    display: grid;
    gap: 3px;
    padding: 9px 10px;
    text-align: left;
    cursor: pointer;
  }

  button:hover:not(:disabled) {
    background: rgba(34, 197, 94, 0.12);
  }

  button:focus-visible {
    outline: 2px solid #22c55e;
    outline-offset: 2px;
  }

  button.blocked {
    color: var(--text-muted, #94a3b8);
    cursor: not-allowed;
  }

  .command-label {
    min-width: 0;
    overflow-wrap: anywhere;
  }
</style>
