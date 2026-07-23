<script>
  const { surfaces = [], selected, onSelect } = $props();

  function handleKeydown(event, index) {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key) || surfaces.length === 0) return;
    event.preventDefault();
    const lastIndex = surfaces.length - 1;
    const nextIndex =
      event.key === 'Home'
        ? 0
        : event.key === 'End'
          ? lastIndex
          : event.key === 'ArrowLeft'
            ? (index - 1 + surfaces.length) % surfaces.length
            : (index + 1) % surfaces.length;
    onSelect?.(surfaces[nextIndex]);
  }
</script>

<div class="surface-selector" role="tablist" aria-label="Model choice surface">
  {#each surfaces as surface, index (surface)}
    <button
      type="button"
      role="tab"
      aria-selected={selected === surface}
      tabindex={selected === surface ? 0 : -1}
      class:active={selected === surface}
      onkeydown={(event) => handleKeydown(event, index)}
      onclick={() => onSelect?.(surface)}
    >
      {surface.replaceAll('_', ' ')}
    </button>
  {:else}
    <p class="empty" role="status">No model choice surfaces available.</p>
  {/each}
</div>

<style>
  .surface-selector {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }

  button {
    min-height: 34px;
    padding: 7px 10px;
    border: 1px solid var(--border-default, #d6d9de);
    border-radius: 6px;
    background: var(--surface-secondary, #f8fafc);
    color: var(--text-primary, #111827);
    font: inherit;
    font-size: 0.75rem;
    font-weight: 700;
    text-transform: capitalize;
    cursor: pointer;
  }

  button.active {
    border-color: var(--primary, #2563eb);
    background: var(--primary-soft, rgba(37, 99, 235, 0.12));
  }

  .empty {
    margin: 0;
    color: var(--text-secondary, #64748b);
    font-size: 0.8rem;
  }
</style>
