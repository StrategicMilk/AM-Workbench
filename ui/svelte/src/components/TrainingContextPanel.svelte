<script>
  let { context = 'inference' } = $props();
</script>

{#if context === 'training'}
  <section class="training-context-panel">
    <slot />
  </section>
{/if}

<style>
  .training-context-panel {
    display: grid;
    gap: 8px;
  }
</style>
