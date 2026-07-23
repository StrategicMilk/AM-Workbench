<script>
  const { routes = [] } = $props();
  const knownDestinations = ['failure_intelligence', 'eval', 'proposal', 'operator_notification'];

  const normalizedRoutes = $derived(
    routes.map((route) => {
      const rawDestination = typeof route.destination === 'string' ? route.destination.trim() : '';
      const destination = knownDestinations.includes(rawDestination) ? rawDestination : 'blocked_unroutable';
      const blockers = Array.isArray(route.blockers) ? route.blockers : [];
      return {
        ...route,
        destination,
        blockers:
          destination === 'blocked_unroutable'
            ? [...blockers, rawDestination ? `Unknown destination: ${rawDestination}` : 'Missing route destination']
            : blockers,
        evidence_refs: Array.isArray(route.evidence_refs) ? route.evidence_refs : [],
        degraded: Boolean(route.degraded) || destination === 'blocked_unroutable',
      };
    }),
  );

  const routeGroups = $derived(
    [...knownDestinations, 'blocked_unroutable'].map((destination) => ({
      destination,
      routes: normalizedRoutes.filter((route) => route.destination === destination),
    })),
  );
</script>

<section class="monitoring-alert-routes" aria-label="Production AI monitoring alert routes">
  {#if normalizedRoutes.length === 0}
    <p class="empty-state" role="status">No alert routes.</p>
  {:else}
    <div class="route-groups">
      {#each routeGroups as group (group.destination)}
        <section class="route-group" aria-labelledby={`monitoring-route-${group.destination}`}>
          <h3 id={`monitoring-route-${group.destination}`}>{group.destination}</h3>
          {#if group.routes.length === 0}
            <p class="route-empty">No routes.</p>
          {:else}
            <div class="route-list">
              {#each group.routes as route (route.signal_id)}
                <article class:degraded={route.degraded} class="route-row">
                  <div class="route-main">
                    <strong>{route.signal_id}</strong>
                    <span>{route.artifact_id || 'pending'}</span>
                  </div>
                  <dl class="route-facts">
                    <div>
                      <dt>State</dt>
                      <dd>{route.degraded ? 'Degraded' : 'Routed'}</dd>
                    </div>
                    <div>
                      <dt>Evidence</dt>
                      <dd>{route.evidence_refs.length}</dd>
                    </div>
                    <div>
                      <dt>Blockers</dt>
                      <dd>{route.blockers.length}</dd>
                    </div>
                  </dl>
                  {#if route.blockers.length > 0}
                    <p class="blocker-text">{route.blockers.join('; ')}</p>
                  {/if}
                </article>
              {/each}
            </div>
          {/if}
        </section>
      {/each}
    </div>
  {/if}
</section>

<style>
  .monitoring-alert-routes {
    width: 100%;
  }

  .route-groups {
    display: grid;
    gap: 12px;
  }

  .route-group h3 {
    margin: 0 0 6px;
    color: var(--text-primary, #111827);
    font-size: 0.875rem;
    font-weight: 800;
  }

  .route-list {
    display: grid;
    gap: 8px;
  }

  .route-row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 10px;
    min-height: 68px;
    padding: 10px 12px;
    border: 1px solid var(--border-default, #d6d9de);
    border-radius: 6px;
    background: var(--surface-primary, #fff);
  }

  .route-row.degraded {
    border-color: var(--danger, #dc2626);
    box-shadow: inset 3px 0 0 var(--danger, #dc2626);
  }

  .route-main {
    min-width: 0;
  }

  .route-main strong {
    display: block;
    color: var(--text-primary, #111827);
    font-size: 0.875rem;
    overflow-wrap: anywhere;
  }

  .route-main span {
    display: block;
    margin-top: 4px;
    color: var(--text-muted, #4b5563);
    font-size: 0.75rem;
    overflow-wrap: anywhere;
  }

  .route-facts {
    display: grid;
    grid-template-columns: repeat(3, minmax(58px, auto));
    gap: 8px;
    margin: 0;
  }

  .route-facts dt {
    color: var(--text-muted, #6b7280);
    font-size: 0.6875rem;
    line-height: 1.2;
  }

  .route-facts dd {
    margin: 2px 0 0;
    color: var(--text-primary, #111827);
    font-size: 0.8125rem;
    font-weight: 700;
  }

  .blocker-text {
    grid-column: 1 / -1;
    margin: 0;
    color: var(--danger, #b91c1c);
    font-size: 0.75rem;
    line-height: 1.4;
    overflow-wrap: anywhere;
  }

  .route-empty,
  .empty-state {
    margin: 0;
    padding: 10px 12px;
    border: 1px solid var(--border-default, #d6d9de);
    border-radius: 6px;
    color: var(--text-muted, #4b5563);
    font-size: 0.8125rem;
  }

  @media (max-width: 720px) {
    .route-row {
      grid-template-columns: 1fr;
    }

    .route-facts {
      grid-template-columns: repeat(3, minmax(0, 1fr));
    }
  }
</style>
