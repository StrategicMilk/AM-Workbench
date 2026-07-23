<script>
  let { as = 'span', class: className = '', children, ...rest } = $props();

  let hiddenClass = $derived(['sr-only', className].filter(Boolean).join(' '));
</script>

<svelte:element this={as} {...rest} class={hiddenClass}>
  {@render children?.()}
</svelte:element>

<style>
  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
    border: 0;
  }
</style>
