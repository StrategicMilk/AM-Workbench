<script>
  let { status = null } = $props();
  let crashGate = $derived((status?.gates ?? []).find((gate) => gate.name === 'crash_recovery' && gate.passed === false));
  let visible = $derived(status?.mode === 'desktop_default' && crashGate);
</script>

{#if visible}
  <aside class="crash-banner" role="alert" data-testid="launcher-crash-recovery">
    <strong>Crash recovery needed</strong>
    <span>{crashGate.remediation || crashGate.blockers?.join(', ') || 'Review the launcher recovery state.'}</span>
  </aside>
{/if}

<style>
  .crash-banner {
    display: grid;
    gap: 4px;
    border: 1px solid #f59e0b;
    border-radius: 8px;
    padding: 12px;
    color: #fde68a;
    background: rgba(120, 53, 15, 0.28);
  }
</style>
