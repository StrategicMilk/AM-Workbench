<script>
  import FocusTrap from '$components/FocusTrap.svelte';
  import { redactSupplyChainValue } from '$lib/security';

  let { result = null, open = false, onClose = () => {} } = $props();
</script>

{#if open}
  <FocusTrap active={open}>
    <div class="bundle-dialog" role="dialog" aria-label="Update support bundle result">
      <h3>Support Bundle</h3>
      {#if result}
        <p>{result.state} {result.bundle_path ? redactSupplyChainValue(result.bundle_path, 'bundle_path') : 'not created'}</p>
        <p>redacted={(result.redacted_files ?? []).length} included={(result.included_files ?? []).length}</p>
      {:else}
        <p>support_bundle has not been created.</p>
      {/if}
      <button onclick={onClose}>Close</button>
    </div>
  </FocusTrap>
{/if}

<style>
  .bundle-dialog {
    border: 1px solid var(--border-default);
    border-radius: 6px;
    padding: 12px;
    background: var(--surface-elevated, #111827);
  }
  h3 {
    margin: 0 0 8px;
    font-size: 15px;
  }
  p {
    overflow-wrap: anywhere;
  }
  button {
    min-height: 44px;
    border: 1px solid var(--border-default);
    border-radius: 6px;
    background: var(--surface-base, #0b1020);
    color: var(--text-primary);
    padding: 6px 10px;
  }
</style>
