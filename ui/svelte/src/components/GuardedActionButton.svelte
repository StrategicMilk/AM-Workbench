<script>
  let { guardrail = { status: 'unknown' }, auth = { permitted: false }, label = 'Run', onclick = null } = $props();

  const disabled = $derived(guardrail.status !== 'ok' || auth.permitted !== true);
</script>

<button type="button" disabled={disabled} aria-disabled={disabled} onclick={disabled ? undefined : onclick}>
  {label}
</button>

<style>
  button {
    min-height: 44px;
  }
</style>
