<script>
  import AssetTaintBadge from './AssetTaintBadge.svelte';

  let { asset } = $props();
  let taints = $derived(asset?.taints ?? []);
</script>

{#if asset}
  <article class="asset-card" data-testid="asset-card-{asset.asset_id}" aria-label="Asset {asset.asset_id}">
    <div class="asset-top">
      <span class="kind">{asset.kind}</span>
      <span class="revision">{asset.revision}</span>
    </div>
    <h3>{asset.name}</h3>
    <p class="mono">{asset.asset_id}</p>
    <p>{asset.created_at_utc}</p>
    <div class="taints">
      {#each taints as taint (taint.taint_id)}
        <AssetTaintBadge {taint} />
      {:else}
        <span class="quiet">No taints</span>
      {/each}
    </div>
  </article>
{/if}

<style>
  .asset-card { min-width: 230px; border: 1px solid var(--border-default); border-radius: 8px; background: var(--surface-elevated); padding: 12px; display: flex; flex-direction: column; gap: 6px; }
  .asset-top { display: flex; justify-content: space-between; gap: 8px; font-size: 0.72rem; color: var(--text-muted); text-transform: uppercase; }
  h3 { margin: 0; font-size: 0.93rem; color: var(--text-primary); }
  p { margin: 0; font-size: 0.78rem; color: var(--text-secondary); }
  .mono, .revision { font-family: var(--font-mono); }
  .taints { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 4px; }
  .quiet { color: var(--text-muted); font-size: 0.74rem; }
</style>
