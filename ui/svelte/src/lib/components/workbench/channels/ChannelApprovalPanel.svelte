<script>
  let { result = null } = $props();
  let authorization = $derived(result?.envelope?.authorization ?? result?.authorization ?? {});
  let approval = $derived(authorization?.approval ?? null);
  let trace = $derived(approval?.ordered_trace ?? []);
</script>

<section class="approval-panel" aria-label="Channel approval">
  <header><h2>Approval</h2><span role="status" aria-label={`Approval outcome ${approval?.outcome ?? 'not requested'}`} aria-live="polite">{approval?.outcome ?? 'not requested'}</span></header>
  {#if approval}
    <dl>
      <div><dt>Matched</dt><dd>{approval.matched_step}</dd></div>
      <div><dt>Fallback</dt><dd>{approval.fallback_rule}</dd></div>
      <div><dt>Fingerprint</dt><dd>{approval.action_fingerprint}</dd></div>
    </dl>
    <div class="trace">
      {#each trace as step}
        <div>{step.name}</div><div>{step.status}</div><div>{step.reason}</div>
      {/each}
    </div>
  {:else}
    <p role="status" aria-live="polite">No approval decision has been attached to the selected channel action.</p>
  {/if}
</section>

<style>
  .approval-panel { display: flex; flex-direction: column; gap: 12px; }
  header { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
  h2, p { margin: 0; }
  header span, p { color: var(--text-muted); }
  dl { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; margin: 0; }
  dl div { border: 1px solid var(--border-default); border-radius: 8px; padding: 10px; background: var(--surface-elevated); }
  dt { color: var(--text-muted); font-size: 12px; }
  dd { margin: 4px 0 0; overflow-wrap: anywhere; }
  .trace { display: grid; grid-template-columns: minmax(120px, 1fr) minmax(100px, .7fr) minmax(160px, 1fr); border: 1px solid var(--border-default); border-radius: 8px; overflow: hidden; }
  .trace div { padding: 8px 10px; border-bottom: 1px solid var(--border-default); }
  @media (max-width: 760px) { dl, .trace { grid-template-columns: 1fr; } }
</style>
