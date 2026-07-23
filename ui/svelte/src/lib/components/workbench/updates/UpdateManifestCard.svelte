<script>
  let { readiness = null } = $props();
  let manifest = $derived(readiness?.manifest);
  let integrity = $derived(readiness?.integrity);
</script>

<section class="manifest-card" aria-label="Update manifest summary" data-state={readiness?.state ?? 'blocked'}>
  <h3>Manifest</h3>
  <dl>
    <div><dt>current</dt><dd>{readiness?.current_version || 'unknown'}</dd></div>
    <div><dt>candidate</dt><dd>{readiness?.candidate_version || 'none'}</dd></div>
    <div><dt>release_notes</dt><dd>{readiness?.release_notes || manifest?.release_notes || 'unavailable'}</dd></div>
    <div><dt>public_export</dt><dd>{readiness?.public_export_ref || manifest?.public_export?.export_ref || 'unavailable'}</dd></div>
    <div><dt>integrity</dt><dd>{integrity?.state || 'unavailable'} {integrity?.passed ? 'verified' : 'blocked'}</dd></div>
  </dl>
  <ul>
    {#each readiness?.reasons ?? [] as reason}
      <li>{reason}</li>
    {/each}
  </ul>
</section>

<style>
  .manifest-card {
    border: 1px solid var(--border-default);
    border-radius: 6px;
    padding: 12px;
    background: var(--surface-elevated, #111827);
  }
  h3 {
    margin: 0 0 8px;
    font-size: 15px;
  }
  dl {
    display: grid;
    gap: 7px;
    margin: 0;
  }
  dl div {
    display: grid;
    grid-template-columns: 110px 1fr;
    gap: 8px;
  }
  dt {
    color: var(--text-muted);
  }
  dd, li {
    margin: 0;
    overflow-wrap: anywhere;
  }
  ul {
    margin: 10px 0 0;
    padding-left: 18px;
  }
  [data-state="ready"] {
    border-color: #31a66a;
  }
  [data-state="blocked"] {
    border-color: #d6a821;
  }
</style>
