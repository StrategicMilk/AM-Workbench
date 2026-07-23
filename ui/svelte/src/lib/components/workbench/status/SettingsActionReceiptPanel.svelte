<script>
  let { result = null } = $props();
  let state = $derived(result?.state ?? 'proposal_only');
  let reasons = $derived(Array.isArray(result?.reasons) ? result.reasons : []);
</script>

<section class="receipt-panel" aria-label="Settings action receipt" data-state={state} role={state === 'applied' ? 'status' : 'alert'} aria-live="polite">
  <h2>Action Result</h2>
  {#if result}
    <p>{result.action_id}: {result.state}</p>
    <p>receipt {result.receipt_id || 'not recorded'} approval {result.approval_decision_ref || 'missing'}</p>
    <ul>
      {#each reasons as reason}
        <li>{reason}</li>
      {:else}
        <li>No reasons recorded.</li>
      {/each}
    </ul>
  {:else}
    <p>proposal_only until an explicit Approval Chain decision and receipt gate pass.</p>
  {/if}
</section>

<style>
  .receipt-panel {
    border: 1px solid var(--border-default);
    border-radius: 6px;
    padding: 12px;
    background: var(--surface-elevated, #111827);
  }
  h2 { margin: 0 0 8px; font-size: 16px; }
  p, li { overflow-wrap: anywhere; }
  [data-state="applied"] { border-color: #31a66a; }
  [data-state="blocked"], [data-state="proposal_only"] { border-color: #d6a821; }
</style>
