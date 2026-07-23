<script>
  let { result = null, busy = false, onRun = () => {} } = $props();
  let confirmed = $state(false);
  let actionId = $derived(result?.fix_action || result?.settings_target || '');
</script>

<aside class="fix-drawer" aria-label="Status fix action">
  {#if result}
    <h2>{result.domain}</h2>
    <p>{result.summary}</p>
    <dl>
      <div><dt>state</dt><dd>{result.state}</dd></div>
      <div><dt>settings</dt><dd>{result.settings_target || 'none'}</dd></div>
      <div><dt>fix</dt><dd>{result.fix_action || 'none'}</dd></div>
    </dl>
    <label class="confirm">
      <input type="checkbox" bind:checked={confirmed} />
      <span>Require Approval Chain decision and receipt before any write callback</span>
    </label>
    <button onclick={() => onRun(actionId)} disabled={!confirmed || !actionId || busy}>
      {busy ? 'Submitting' : 'Submit Action'}
    </button>
  {:else}
    <h2>Action</h2>
    <p>Select a health row to inspect its settings target or fix action.</p>
  {/if}
</aside>

<style>
  .fix-drawer {
    min-height: 260px;
    border: 1px solid var(--border-default);
    border-radius: 6px;
    padding: 12px;
    background: var(--surface-elevated, #111827);
  }
  h2 { margin: 0 0 8px; font-size: 16px; }
  p { color: var(--text-muted); overflow-wrap: anywhere; }
  dl { display: grid; gap: 8px; margin: 12px 0; }
  dl div { display: grid; grid-template-columns: 80px 1fr; gap: 8px; }
  dt { color: var(--text-muted); }
  dd { margin: 0; overflow-wrap: anywhere; }
  .confirm { display: flex; gap: 8px; align-items: flex-start; margin: 12px 0; }
  button {
    width: 100%;
    min-height: 38px;
    border: 1px solid var(--border-default);
    border-radius: 6px;
    background: var(--accent-primary, #4f8cff);
    color: white;
    font: inherit;
  }
  button:disabled { opacity: 0.5; }
</style>
