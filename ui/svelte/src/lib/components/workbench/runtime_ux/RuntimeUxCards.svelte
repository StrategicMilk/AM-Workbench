<script>
  import RunHandlePanel from './RunHandlePanel.svelte';
  import { createRuntimeUxState } from './runtimeUxStore.svelte.js';

  let { runtimeUx = null } = $props();
  let store = $state(null);
  $effect(() => {
    if (runtimeUx === null && store === null) {
      store = createRuntimeUxState();
    }
  });
  let incoming = $derived(runtimeUx ?? store?.state ?? { cards: [], run: null, events: [] });
  let cards = $derived(incoming.cards ?? []);
  let run = $derived(incoming.run ?? null);
  let events = $derived(incoming.events ?? []);
  let summary = $derived({
    blocked: cards.filter((card) => ['blocked', 'unavailable', 'replay_mismatch'].includes(card.status)).length,
    degraded: cards.filter((card) => ['approval_required', 'degraded', 'stale'].includes(card.status)).length,
    allowed: cards.filter((card) => card.status === 'allowed').length
  });

  const severityClass = (status) => {
    if (status === 'allowed') return 'allowed';
    if (status === 'approval_required' || status === 'degraded' || status === 'stale') return 'degraded';
    return 'blocked';
  };
</script>

<section class="runtime-ux-cards" aria-label="Workbench runtime UX" data-testid="runtime-ux-cards">
  <header>
    <h2>Runtime</h2>
    <span role="status" aria-label={`Runtime summary ${summary.blocked} blocked, ${summary.degraded} degraded, ${summary.allowed} allowed`}>
      {summary.blocked} blocked / {summary.degraded} degraded / {summary.allowed} allowed
    </span>
  </header>

  <RunHandlePanel runHandle={run} {events} />

  <div class="runtime-grid">
    {#each cards as card (card.id)}
      <article
        class={severityClass(card.status)}
        data-runtime-state={card.status}
        role={severityClass(card.status) === 'blocked' ? 'alert' : 'status'}
        aria-label={`${card.label}: ${card.status}`}
      >
        <strong>{card.label}</strong>
        <span>{card.status}</span>
        <p>{card.detail}</p>
      </article>
    {/each}
  </div>
</section>

<style>
  .runtime-ux-cards {
    display: grid;
    gap: 10px;
    border-top: 1px solid var(--border-default, #334155);
    padding: 10px;
  }

  header {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 10px;
  }

  h2 {
    margin: 0;
    font-size: 0.92rem;
  }

  header span {
    color: var(--text-muted, #94a3b8);
    font-size: 0.72rem;
  }

  .runtime-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 8px;
  }

  article {
    display: grid;
    min-height: 76px;
    gap: 4px;
    border: 1px solid var(--border-default, #334155);
    border-radius: 8px;
    padding: 8px;
    background: rgba(15, 23, 42, 0.5);
  }

  article.allowed {
    border-color: #22c55e;
  }

  article.degraded {
    border-color: #f59e0b;
  }

  article.blocked {
    border-color: #ef4444;
  }

  strong {
    color: var(--text-primary, #e5e7eb);
    font-size: 0.8rem;
  }

  article span,
  p {
    margin: 0;
    color: var(--text-muted, #94a3b8);
    font-size: 0.72rem;
    overflow-wrap: anywhere;
  }
</style>
