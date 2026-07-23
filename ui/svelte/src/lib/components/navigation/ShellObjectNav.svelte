<script>
  import { requireEvidence } from '$lib/evidence/evidenceGuard.js';

  let { items = [], onNavigate = () => {} } = $props();

  function normalizeItem(item) {
    const evidenceRefs = Array.isArray(item.evidence_refs) ? item.evidence_refs : [];
    if (evidenceRefs.length === 0) {
      return {
        ...item,
        evidenceBlocked: true,
        evidenceBlockReason: `Evidence guard: shell-object-nav:${item.view ?? item.label ?? 'item'} is missing evidence refs`,
      };
    }
    try {
      requireEvidence(evidenceRefs, `shell-object-nav:${item.view ?? item.label ?? 'item'}`);
      return { ...item, evidenceBlocked: false };
    } catch (error) {
      return { ...item, evidenceBlocked: true, evidenceBlockReason: error.message };
    }
  }

  let navItems = $derived(items.map(normalizeItem));
</script>

<nav class="shell-object-nav" aria-label="Workbench object navigation" data-testid="workbench-shell-nav">
  {#each navItems as item (item.view)}
    <button
      type="button"
      class:active={item.active}
      aria-pressed={item.active}
      aria-label={`${item.label}: ${item.count} ${item.why ?? ''}`.trim()}
      title={item.evidenceBlocked ? item.evidenceBlockReason : item.why}
      disabled={item.evidenceBlocked}
      onclick={() => !item.evidenceBlocked && onNavigate(item.view)}
    >
      <span>{item.label}</span>
      <strong>{item.count}</strong>
    </button>
  {/each}
</nav>

<style>
  .shell-object-nav {
    display: flex;
    gap: 6px;
    overflow-x: auto;
    padding: 2px;
  }

  button {
    min-width: 96px;
    border: 1px solid var(--border-default, #334155);
    border-radius: 6px;
    background: var(--surface-elevated, #111827);
    color: var(--text-primary, #e5e7eb);
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    padding: 8px 10px;
    font: inherit;
    cursor: pointer;
  }

  button.active,
  button:hover {
    border-color: #38bdf8;
    background: rgba(56, 189, 248, 0.12);
  }

  button:disabled {
    cursor: not-allowed;
    opacity: 0.55;
  }

  button:focus-visible {
    outline: 2px solid #38bdf8;
    outline-offset: 2px;
  }

  strong {
    font-size: 0.78rem;
    color: var(--text-muted, #94a3b8);
  }
</style>
