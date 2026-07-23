<script>
  let { enabled = false, label = 'Unavailable' } = $props();
</script>

{#if enabled}
  <slot />
{:else}
  <button type="button" disabled aria-disabled="true">{label}</button>
{/if}

<style>
  button {
    min-height: 44px;
  }
</style>
