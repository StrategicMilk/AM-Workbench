<script>
  import { workbenchKernelRequest } from '$lib/api.js';

  const KAIZEN_API = '/api/v1/workbench/kaizen';

  let report = $state(null);
  let improvements = $state([]);
  let defectTrends = $state(null);
  let error = $state('');

  function payloadOf(body) {
    return body?.payload ?? body ?? {};
  }

  async function loadKaizen() {
    try {
      const [reportBody, improvementsBody, trendsBody] = await Promise.all([
        workbenchKernelRequest(`${KAIZEN_API}/report`),
        workbenchKernelRequest(`${KAIZEN_API}/improvements`),
        workbenchKernelRequest(`${KAIZEN_API}/defect-trends`),
      ]);
      const reportPayload = payloadOf(reportBody);
      const improvementsPayload = payloadOf(improvementsBody);
      const trendsPayload = payloadOf(trendsBody);
      report = reportPayload.report ?? reportPayload;
      improvements = Array.isArray(improvementsPayload)
        ? improvementsPayload
        : improvementsPayload.improvements ?? improvementsPayload.items ?? [];
      defectTrends = trendsPayload.defect_trends ?? trendsPayload;
      error = '';
    } catch (err) {
      error = err instanceof Error ? err.message : 'kaizen load failed';
    }
  }

  $effect(() => {
    loadKaizen();
  });
</script>

<section class="kaizen-view" aria-label="Kaizen workbench">
  <header>
    <h1>Kaizen</h1>
    <button type="button" onclick={loadKaizen}>Refresh</button>
  </header>

  {#if error}
    <p role="status" class="error">{error}</p>
  {/if}

  <div class="summary-grid">
    <article>
      <span>Proposed</span>
      <strong>{report?.total_proposed ?? 0}</strong>
    </article>
    <article>
      <span>Active</span>
      <strong>{report?.total_active ?? 0}</strong>
    </article>
    <article>
      <span>Confirmed</span>
      <strong>{report?.total_confirmed ?? 0}</strong>
    </article>
    <article>
      <span>Failed</span>
      <strong>{report?.total_failed ?? 0}</strong>
    </article>
  </div>

  <section aria-label="Kaizen improvements">
    <h2>Improvements</h2>
    {#if improvements.length === 0}
      <p role="status">No improvements recorded.</p>
    {:else}
      <table>
        <thead>
          <tr><th>ID</th><th>Metric</th><th>Status</th><th>Effect</th><th>Evidence</th></tr>
        </thead>
        <tbody>
          {#each improvements as item}
            <tr>
              <td>{item.id}</td>
              <td>{item.metric}</td>
              <td>{item.status}</td>
              <td>{item.actual_value ?? item.target_value ?? 'pending'}</td>
              <td>{item.evidence_refs?.length ? item.evidence_refs.join(', ') : 'no evidence'}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    {/if}
  </section>

  <section aria-label="Kaizen defect trends">
    <h2>Defect Trends</h2>
    <pre>{JSON.stringify(defectTrends?.trends ?? {}, null, 2)}</pre>
  </section>
</section>

<style>
  .kaizen-view {
    display: grid;
    gap: 1rem;
    padding: 1rem;
  }

  header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 1rem;
  }

  .summary-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(8rem, 1fr));
    gap: 0.75rem;
  }

  article {
    border: 1px solid var(--color-border, #d0d7de);
    border-radius: 8px;
    padding: 0.75rem;
  }

  article span {
    display: block;
    color: var(--color-text-muted, #57606a);
    font-size: 0.85rem;
  }

  article strong {
    display: block;
    margin-top: 0.25rem;
    font-size: 1.4rem;
  }

  table {
    width: 100%;
    border-collapse: collapse;
  }

  th,
  td {
    border-bottom: 1px solid var(--color-border, #d0d7de);
    padding: 0.5rem;
    text-align: left;
  }

  .error {
    color: var(--color-danger, #b42318);
  }
</style>
