<script>
  let { run = null, hardware, policy, provenance } = $props();
  let metrics = $derived(run?.metrics ?? []);
  let revisions = $derived(run?.asset_revisions ?? []);
</script>

<section class="run-detail" aria-label="Selected run detail">
  {#if run}
    <div class="detail-head">
      <h3>{run.run_id}</h3>
      <span>{run.kind}</span>
    </div>
    <dl>
      <dt>Status</dt><dd>{run.status}</dd>
      <dt>Started</dt><dd>{run.started_at_utc}</dd>
      <dt>Finished</dt><dd>{run.finished_at_utc || 'active'}</dd>
      <dt>Lease</dt><dd>{run.lease_id || 'none'}</dd>
      <dt>Shard</dt><dd>{run.shard_kind || 'none'}</dd>
    </dl>
    <div class="slot-row">
      {#if hardware}
        {@render hardware()}
      {/if}
      {#if policy}
        {@render policy()}
      {/if}
      {#if provenance}
        {@render provenance()}
      {/if}
    </div>
    <h4>Metrics</h4>
    <ul>
      {#each metrics as metric (`${metric.name}-${metric.unit}`)}
        <li><span>{metric.name}</span><strong>{metric.value}{metric.unit ? ` ${metric.unit}` : ''}</strong></li>
      {:else}
        <li class="muted">No metrics recorded.</li>
      {/each}
    </ul>
    <h4>Asset revisions</h4>
    <ul>
      {#each revisions as [assetId, revision] (`${assetId}-${revision}`)}
        <li><span>{assetId}</span><strong>{revision}</strong></li>
      {:else}
        <li class="muted">No asset revisions linked.</li>
      {/each}
    </ul>
  {:else}
    <div class="empty">Select a run to inspect metrics, trace links, and evidence.</div>
  {/if}
</section>

<style>
  .run-detail { border: 1px solid var(--border-default); border-radius: 8px; padding: 14px; background: var(--surface-elevated); min-height: 220px; }
  .detail-head { display: flex; justify-content: space-between; gap: 12px; align-items: center; margin-bottom: 10px; }
  h3, h4 { margin: 0; color: var(--text-primary); }
  h3 { font-size: 1rem; font-family: var(--font-mono); }
  h4 { font-size: 0.78rem; margin-top: 14px; text-transform: uppercase; color: var(--text-muted); }
  dl { display: grid; grid-template-columns: 90px 1fr; gap: 6px 10px; margin: 0; font-size: 0.8rem; }
  dt { color: var(--text-muted); }
  dd { margin: 0; color: var(--text-primary); overflow-wrap: anywhere; }
  ul { list-style: none; padding: 0; margin: 8px 0 0; display: flex; flex-direction: column; gap: 5px; }
  li { display: flex; justify-content: space-between; gap: 12px; font-size: 0.8rem; color: var(--text-secondary); }
  strong { color: var(--text-primary); font-family: var(--font-mono); }
  .slot-row { display: flex; flex-wrap: wrap; gap: 6px; margin: 12px 0; }
  .empty, .muted { color: var(--text-muted); }
</style>
