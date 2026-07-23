<script>
  import { getFullSpectrumAuditResults, getFullSpectrumAuditRun } from '$lib/api.js';
  import { showToast } from '$lib/stores/toast.svelte.js';

  const severityOptions = ['critical', 'high', 'medium', 'low', 'info'];
  const statusOptions = ['open', 'all', 'closed'];

  let limit = $state(10);
  let includeArchived = $state(false);
  let findingLimit = $state(50);
  let findingStatus = $state('open');
  let severity = $state('');
  let lane = $state('');
  let query = $state('');
  let payload = $state(null);
  let runDetail = $state(null);
  let loading = $state(true);
  let detailLoading = $state(false);
  let error = $state('');
  let detailError = $state('');
  let selectedRunId = $state(null);

  let runs = $derived(payload?.runs ?? []);
  let selectedRun = $derived(runs.find((run) => run.run_id === selectedRunId) ?? runs[0] ?? null);
  let detailRun = $derived(runDetail?.run ?? selectedRun);
  let findings = $derived(runDetail?.run?.findings ?? selectedRun?.top_findings ?? []);
  let laneOptions = $derived(Object.keys(detailRun?.lane_counts ?? {}).sort());
  let totalOpenFindings = $derived(payload?.summary?.open_findings ?? 0);
  let totalFindings = $derived(payload?.summary?.total_findings ?? 0);

  function countLabel(map, key) {
    return map?.[key] ?? 0;
  }

  async function loadRuns(requestedLimit = limit, requestedArchived = includeArchived) {
    loading = true;
    error = '';
    try {
      const data = await getFullSpectrumAuditResults({ limit: requestedLimit, includeArchived: requestedArchived });
      payload = data;
      selectedRunId = data.runs?.[0]?.run_id ?? null;
    } catch (err) {
      error = err.message ?? String(err);
      showToast(`Audit results load failed: ${error}`, 'error');
    } finally {
      loading = false;
    }
  }

  async function loadRunDetail(runId, options) {
    detailLoading = true;
    detailError = '';
    try {
      const data = await getFullSpectrumAuditRun(runId, options);
      if (data.status !== 'ok') {
        throw new Error(data.error ?? 'audit run unavailable');
      }
      if (runId === selectedRunId) {
        runDetail = data;
      }
    } catch (err) {
      detailError = err.message ?? String(err);
      runDetail = null;
      showToast(`Audit run load failed: ${detailError}`, 'error');
    } finally {
      detailLoading = false;
    }
  }

  function selectRun(runId) {
    selectedRunId = runId;
    runDetail = null;
    detailError = '';
  }

  $effect(() => {
    const requestedLimit = limit;
    const requestedArchived = includeArchived;
    loadRuns(requestedLimit, requestedArchived);
  });

  $effect(() => {
    const runId = selectedRunId;
    const options = {
      findingLimit,
      includeArchived,
      findingStatus,
      severity,
      lane,
      query,
    };
    if (runId) {
      loadRunDetail(runId, options);
    }
  });
</script>

<section class="audit-results-view" aria-label="Full-spectrum audit results">
  <header class="audit-toolbar">
    <div>
      <h1>Audit Results</h1>
      <p>Full-spectrum runs</p>
    </div>
    <div class="audit-controls" aria-label="Audit result controls">
      <label>
        <span>Runs</span>
        <input bind:value={limit} type="number" min="1" max="50" aria-label="Audit result run limit" />
      </label>
      <label class="toggle">
        <input bind:checked={includeArchived} type="checkbox" />
        <span>Archived</span>
      </label>
      <button type="button" onclick={() => loadRuns(limit, includeArchived)} disabled={loading}>
        <i class="fas fa-sync-alt" class:fa-spin={loading} aria-hidden="true"></i>
        Refresh
      </button>
    </div>
  </header>

  {#if loading}
    <div class="state" role="status" aria-live="polite">Loading audit results.</div>
  {:else if error}
    <div class="state error" role="alert">{error}</div>
  {:else if runs.length === 0}
    <div class="state empty" role="status">No full-spectrum audit runs found.</div>
  {:else}
    <section class="summary-strip" aria-label="Audit result summary">
      <div><strong>{runs.length}</strong><span>Runs</span></div>
      <div><strong>{totalOpenFindings}</strong><span>Open findings</span></div>
      <div><strong>{totalFindings}</strong><span>Total findings</span></div>
      <div><strong>{payload?.summary?.skipped_runs ?? 0}</strong><span>Skipped</span></div>
    </section>

    <div class="audit-workspace">
      <section class="run-list" aria-label="Audit run list">
        {#each runs as run}
          <button
            type="button"
            class:selected={selectedRun?.run_id === run.run_id}
            aria-pressed={selectedRun?.run_id === run.run_id}
            onclick={() => selectRun(run.run_id)}
          >
            <span class="run-id">{run.run_id}</span>
            <span>{run.status}</span>
            <strong>{run.open_findings} open</strong>
          </button>
        {/each}
      </section>

      {#if detailRun}
        <article class="run-detail" aria-label="Selected audit run detail" aria-busy={detailLoading}>
          <header>
            <div>
              <h2>{detailRun.run_id}</h2>
              <p>{detailRun.phase ?? detailRun.status} - {detailRun.completed_at ?? detailRun.started_at ?? 'unknown time'}</p>
            </div>
            <span class:pinned={detailRun.pinned}>{detailRun.archived ? 'archived' : detailRun.pinned ? 'pinned' : 'active'}</span>
          </header>

          <section class="metric-grid" aria-label="Selected run metrics">
            <div><span>Findings</span><strong>{detailRun.finding_count}</strong></div>
            <div><span>Open</span><strong>{detailRun.open_findings}</strong></div>
            <div><span>Critical</span><strong>{countLabel(detailRun.severity_counts, 'critical')}</strong></div>
            <div><span>High</span><strong>{countLabel(detailRun.severity_counts, 'high')}</strong></div>
          </section>

          <section class="finding-filters" aria-label="Finding filters">
            <label>
              <span>Status</span>
              <select bind:value={findingStatus} aria-label="Finding status filter">
                {#each statusOptions as option}
                  <option value={option}>{option}</option>
                {/each}
              </select>
            </label>
            <label>
              <span>Severity</span>
              <select bind:value={severity} aria-label="Finding severity filter">
                <option value="">all</option>
                {#each severityOptions as option}
                  <option value={option}>{option}</option>
                {/each}
              </select>
            </label>
            <label>
              <span>Lane</span>
              <select bind:value={lane} aria-label="Finding lane filter">
                <option value="">all</option>
                {#each laneOptions as option}
                  <option value={option}>{option}</option>
                {/each}
              </select>
            </label>
            <label>
              <span>Search</span>
              <input bind:value={query} type="search" aria-label="Finding text search" autocomplete="off" />
            </label>
            <label>
              <span>Limit</span>
              <input bind:value={findingLimit} type="number" min="1" max="250" aria-label="Finding result limit" />
            </label>
          </section>

          {#if detailError}
            <div class="state error" role="alert">{detailError}</div>
          {/if}

          <section class="artifact-list" aria-label="Audit artifacts">
            <h3>Artifacts</h3>
            {#each detailRun.artifact_refs ?? [] as artifact}
              <code>{artifact}</code>
            {/each}
          </section>

          <section class="lane-list" aria-label="Lane artifacts">
            <h3>Lane Evidence</h3>
            {#each detailRun.lane_artifacts ?? [] as artifact}
              <code>{artifact.lane}: {artifact.path}</code>
            {:else}
              <p role="status">No lane artifact index.</p>
            {/each}
          </section>

          <section class="finding-list" aria-label="Filtered findings">
            <div class="section-heading">
              <h3>Findings</h3>
              <span>{detailRun.finding_result_count ?? findings.length} matches</span>
            </div>
            {#if detailLoading}
              <div class="state" role="status" aria-live="polite">Loading findings.</div>
            {:else if findings.length === 0}
              <p role="status">No findings match the current filters.</p>
            {:else}
              <table class="finding-table" aria-label="Filtered full-spectrum findings">
                <thead>
                  <tr>
                    <th scope="col">ID</th>
                    <th scope="col">Severity</th>
                    <th scope="col">Lane</th>
                    <th scope="col">Status</th>
                    <th scope="col">Finding</th>
                  </tr>
                </thead>
                <tbody>
                  {#each findings as finding}
                    <tr>
                      <td><code>{finding.id}</code></td>
                      <td><strong>{finding.severity}</strong></td>
                      <td>{finding.lane}</td>
                      <td>{finding.closure_status ?? finding.status}</td>
                      <td>{finding.title}</td>
                    </tr>
                  {/each}
                </tbody>
              </table>
            {/if}
          </section>
        </article>
      {/if}
    </div>
  {/if}
</section>

<style>
  .audit-results-view {
    display: grid;
    gap: 16px;
    padding: 20px;
    max-width: 1480px;
  }

  .audit-toolbar {
    display: flex;
    justify-content: space-between;
    gap: 16px;
    align-items: end;
  }

  .audit-toolbar h1,
  .audit-toolbar p {
    margin: 0;
  }

  .audit-toolbar p,
  .summary-strip span,
  .metric-grid span,
  .finding-filters span,
  .section-heading span,
  .finding-table th {
    color: var(--text-muted);
  }

  .audit-controls,
  .finding-filters {
    display: flex;
    gap: 10px;
    align-items: center;
    flex-wrap: wrap;
  }

  .audit-controls label,
  .finding-filters label {
    display: flex;
    align-items: center;
    gap: 6px;
  }

  .audit-controls input[type='number'],
  .finding-filters input[type='number'] {
    width: 72px;
  }

  .finding-filters input[type='search'] {
    width: min(260px, 52vw);
  }

  .audit-controls button,
  .run-list button,
  .finding-filters input,
  .finding-filters select,
  .audit-controls input {
    border: 1px solid var(--border-default, #334155);
    background: var(--surface-elevated, #111827);
    color: var(--text-primary, #e5e7eb);
    border-radius: 8px;
  }

  .audit-controls button {
    min-height: 44px;
    padding: 7px 12px;
  }

  .finding-filters input,
  .finding-filters select,
  .audit-controls input {
    min-height: 44px;
    padding: 6px 8px;
  }

  .state {
    padding: 16px;
    border: 1px solid var(--border-default, #334155);
    border-radius: 8px;
  }

  .state.error {
    color: #fca5a5;
  }

  .summary-strip,
  .metric-grid {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 10px;
  }

  .summary-strip div,
  .metric-grid div {
    min-width: 0;
    border: 1px solid var(--border-default, #334155);
    border-radius: 8px;
    padding: 12px;
    background: var(--surface-elevated, #111827);
  }

  .summary-strip strong,
  .metric-grid strong {
    display: block;
    font-size: 22px;
  }

  .audit-workspace {
    display: grid;
    grid-template-columns: minmax(260px, 360px) minmax(0, 1fr);
    gap: 14px;
    align-items: start;
  }

  .run-list {
    display: grid;
    gap: 8px;
  }

  .run-list button {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 4px 10px;
    text-align: left;
    padding: 10px 12px;
  }

  .run-list button.selected {
    border-color: var(--accent, #60a5fa);
  }

  .run-id {
    overflow-wrap: anywhere;
  }

  .run-detail {
    display: grid;
    gap: 14px;
    min-width: 0;
  }

  .run-detail header,
  .section-heading {
    display: flex;
    justify-content: space-between;
    gap: 12px;
  }

  .run-detail h2,
  .run-detail h3,
  .run-detail p {
    margin: 0;
  }

  .run-detail header span {
    align-self: start;
    border: 1px solid var(--border-default, #334155);
    border-radius: 999px;
    padding: 4px 10px;
    color: var(--text-muted);
  }

  .run-detail header span.pinned {
    color: var(--success, #86efac);
  }

  .artifact-list,
  .lane-list,
  .finding-list {
    display: grid;
    gap: 8px;
  }

  .artifact-list code,
  .lane-list code {
    overflow-wrap: anywhere;
    border: 1px solid var(--border-default, #334155);
    border-radius: 6px;
    padding: 8px;
  }

  .finding-table th {
    font-size: 12px;
    text-transform: uppercase;
  }

  .finding-table {
    border-collapse: collapse;
    width: 100%;
  }

  .finding-table th,
  .finding-table td {
    min-width: 0;
    overflow-wrap: anywhere;
    border-bottom: 1px solid var(--border-muted, #334155);
    padding: 8px 10px 8px 0;
    text-align: left;
    vertical-align: top;
  }

  @media (max-width: 980px) {
    .audit-toolbar,
    .run-detail header {
      align-items: stretch;
      flex-direction: column;
    }

    .audit-workspace {
      grid-template-columns: 1fr;
    }

    .summary-strip,
    .metric-grid {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }

    .finding-table {
      display: block;
      overflow-x: auto;
    }
  }

  @media (max-width: 560px) {
    .audit-results-view {
      padding: 14px;
    }

    .summary-strip,
    .metric-grid {
      grid-template-columns: 1fr;
    }
  }
</style>
