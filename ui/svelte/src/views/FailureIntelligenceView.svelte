<script>
  import { showToast } from '$lib/stores/toast.svelte.js';
  import { browserLocationParam } from '$lib/utils/browser.js';
  import { workbenchKernelRequest } from '$lib/api.js';

  let { projectId = browserLocationParam('project_id', 'default') } = $props();

  const failureKinds = [
    'missing_capability',
    'stale_source',
    'weak_method',
    'bad_prompt',
    'bad_routing',
    'insufficient_eval',
    'policy_conflict',
    'unavailable_runtime',
    'hallucinated_tool_ability',
    'dataset_drift',
    'user_ambiguity'
  ];
  const followupKinds = [
    'eval_case',
    'method_test',
    'tool_card_update',
    'prompt_patch',
    'policy_change',
    'source_refresh',
    'benchmark_run',
    'capability_pack_issue'
  ];

  let autopsies = $state([]);
  let selectedAutopsyId = $state(null);
  let failureKindFilter = $state(null);
  let followupKindFilter = $state(null);
  let degradedFilter = $state('all');
  let confidenceFloor = $state(0);
  let sourceFilter = $state('');
  let loading = $state(true);
  let error = $state(null);

  let filteredAutopsies = $derived(
    autopsies.filter((row) => {
      const primary = row.candidates?.[0];
      if (failureKindFilter && primary?.failure_kind !== failureKindFilter) return false;
      if (followupKindFilter && row.followup?.kind !== followupKindFilter) return false;
      if (degradedFilter === 'degraded' && !row.degraded) return false;
      if (degradedFilter === 'healthy' && row.degraded) return false;
      if (primary && primary.confidence < Number(confidenceFloor)) return false;
      if (sourceFilter) {
        const haystack = [
          row.run_id,
          row.status,
          row.degraded_reason,
          row.followup?.description,
          ...(row.evidence_refs ?? []),
          ...(primary?.evidence_refs ?? [])
        ].join(' ').toLowerCase();
        if (!haystack.includes(sourceFilter.toLowerCase())) return false;
      }
      return true;
    })
  );
  let selectedAutopsy = $derived(
    filteredAutopsies.find((row) => row.autopsy_id === selectedAutopsyId) ?? filteredAutopsies[0] ?? null
  );
  let countsByFailure = $derived(
    failureKinds.reduce((acc, kind) => {
      acc[kind] = autopsies.filter((row) => row.candidates?.[0]?.failure_kind === kind).length;
      return acc;
    }, {})
  );
  let degradedCount = $derived(autopsies.filter((row) => row.degraded).length);
  let ambiguousCount = $derived(
    autopsies.filter((row) => row.candidates?.length > 1 || row.degraded_reason === 'low_confidence').length
  );

  function confidenceLabel(value) {
    return `${Math.round((Number(value) || 0) * 100)}%`;
  }

  function primaryCandidate(row) {
    return row?.candidates?.[0] ?? null;
  }

  function workbenchUrl(path, params = {}) {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value !== null && value !== undefined && value !== '') query.set(key, value);
    }
    return query.toString() ? `${path}?${query.toString()}` : path;
  }

  $effect(() => {
    let cancelled = false;
    loading = true;
    error = null;
    workbenchKernelRequest(workbenchUrl('/api/workbench/failure-intelligence', { project_id: projectId, followup_kind: followupKindFilter }))
      .then((rows) => {
        if (cancelled) return;
        autopsies = rows;
        if (!selectedAutopsyId && rows.length > 0) selectedAutopsyId = rows[0].autopsy_id;
        loading = false;
      })
      .catch((err) => {
        if (cancelled) return;
        error = err.message ?? String(err);
        showToast(`Failure intelligence load failed: ${error}`, 'error');
        loading = false;
      });
    return () => { cancelled = true; };
  });
</script>

<div class="failure-intelligence">
  <header class="toolbar">
    <div>
      <h2>Failure Intelligence</h2>
      <p>{projectId}</p>
    </div>
    <div class="filters" aria-label="Failure intelligence filters">
      <select bind:value={failureKindFilter} aria-label="Failure kind filter">
        <option value={null}>All failures</option>
        {#each failureKinds as kind}
          <option value={kind}>{kind}</option>
        {/each}
      </select>
      <select bind:value={followupKindFilter} aria-label="Follow-up filter">
        <option value={null}>All follow-ups</option>
        {#each followupKinds as kind}
          <option value={kind}>{kind}</option>
        {/each}
      </select>
      <select bind:value={degradedFilter} aria-label="Degraded filter">
        <option value="all">All states</option>
        <option value="degraded">Degraded</option>
        <option value="healthy">Classified</option>
      </select>
      <label>
        <span>Confidence</span>
        <input bind:value={confidenceFloor} type="range" min="0" max="1" step="0.05" />
        <strong>{confidenceLabel(confidenceFloor)}</strong>
      </label>
      <input bind:value={sourceFilter} placeholder="Source, run, evidence" aria-label="Evidence source filter" />
    </div>
  </header>

  {#if loading}
    <div class="state">Loading autopsies.</div>
  {:else if error}
    <div class="state error">{error}</div>
  {:else if autopsies.length === 0}
    <div class="state empty">No failure intelligence records.</div>
  {:else}
    <section class="summary-band" aria-label="Failure summary">
      <div><strong>{autopsies.length}</strong><span>Autopsies</span></div>
      <div><strong>{degradedCount}</strong><span>Missing evidence</span></div>
      <div><strong>{ambiguousCount}</strong><span>Ambiguous</span></div>
      <div><strong>{filteredAutopsies.length}</strong><span>Filtered</span></div>
    </section>

    <div class="workspace">
      <aside class="failure-lanes" aria-label="Failure kinds">
        {#each failureKinds as kind}
          <button
            class:active={failureKindFilter === kind}
            aria-pressed={failureKindFilter === kind}
            type="button"
            onclick={() => { failureKindFilter = failureKindFilter === kind ? null : kind; }}
          >
            <span>{kind}</span>
            <strong>{countsByFailure[kind] ?? 0}</strong>
          </button>
        {/each}
      </aside>

      <section class="autopsy-list" aria-label="Autopsy list">
        {#each filteredAutopsies as row (row.autopsy_id)}
          {@const primary = primaryCandidate(row)}
          <button
            class:selected={selectedAutopsy?.autopsy_id === row.autopsy_id}
            class:degraded={row.degraded}
            aria-pressed={selectedAutopsy?.autopsy_id === row.autopsy_id}
            type="button"
            onclick={() => { selectedAutopsyId = row.autopsy_id; }}
          >
            <span class="run">{row.run_id || row.autopsy_id}</span>
            <span class="kind">{primary?.failure_kind ?? 'user_ambiguity'}</span>
            <span class="followup">{row.followup?.kind ?? 'prompt_patch'}</span>
            <span class="confidence">{confidenceLabel(primary?.confidence ?? 0)}</span>
          </button>
        {/each}
      </section>

      {#if selectedAutopsy}
        {@const primary = primaryCandidate(selectedAutopsy)}
        <article class="detail-pane" aria-label="Autopsy detail">
          <header>
            <div>
              <h3>{selectedAutopsy.run_id || selectedAutopsy.autopsy_id}</h3>
              <p>{selectedAutopsy.status} · {selectedAutopsy.created_at_utc}</p>
            </div>
            <span class:warning={selectedAutopsy.degraded}>{selectedAutopsy.degraded ? 'degraded' : 'classified'}</span>
          </header>

          {#if selectedAutopsy.degraded}
            <section class="state-row warning">
              <strong>{selectedAutopsy.degraded_reason ?? 'missing_evidence'}</strong>
              <span>Prompt follow-up is required before this can become product knowledge.</span>
            </section>
          {/if}

          {#if selectedAutopsy.candidates.length > 1}
            <section class="state-row">
              <strong>ambiguous classification</strong>
              <span>{selectedAutopsy.candidates.length} ordered candidates require operator review.</span>
            </section>
          {/if}

          <section class="candidate-stack">
            <h4>Candidate failures</h4>
            {#each selectedAutopsy.candidates as candidate}
              <div class="candidate">
                <div>
                  <strong>{candidate.failure_kind}</strong>
                  <span>{confidenceLabel(candidate.confidence)}</span>
                </div>
                <p>{candidate.reason}</p>
                <ul>
                  {#each candidate.evidence_refs as ref}
                    <li>{ref}</li>
                  {/each}
                </ul>
              </div>
            {/each}
          </section>

          <section class="followup-card">
            <h4>Durable follow-up</h4>
            <div class="followup-grid">
              <span>eval</span>
              <span>method</span>
              <span>tool</span>
              <span>policy</span>
              <span>dataset</span>
            </div>
            <strong>{selectedAutopsy.followup.kind}</strong>
            <p>{selectedAutopsy.followup.description}</p>
          </section>

          <section class="evidence">
            <h4>Evidence refs</h4>
            {#if selectedAutopsy.evidence_refs.length === 0}
              <p>Missing evidence.</p>
            {:else}
              <ul>
                {#each selectedAutopsy.evidence_refs as ref}
                  <li>{ref}</li>
                {/each}
              </ul>
            {/if}
          </section>
        </article>
      {:else}
        <div class="state empty">No matching autopsies.</div>
      {/if}
    </div>
  {/if}
</div>

<style>
  .failure-intelligence { padding: 18px; max-width: 1500px; display: flex; flex-direction: column; gap: 14px; color: var(--text-primary); }
  .toolbar { display: flex; justify-content: space-between; gap: 14px; align-items: flex-start; flex-wrap: wrap; }
  h2, h3, h4, p { margin: 0; }
  h2 { font-size: 1.25rem; }
  h3 { font-size: 1rem; }
  h4 { font-size: 0.82rem; margin-bottom: 8px; }
  p, li, span, button, input, select, strong { font-size: 0.82rem; }
  .toolbar p { color: var(--text-muted); font-family: var(--font-mono); margin-top: 3px; }
  .filters { display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; align-items: center; }
  .filters label { display: flex; align-items: center; gap: 6px; min-height: 44px; border: 1px solid var(--border-default); border-radius: 6px; background: var(--surface-elevated); padding: 0 8px; }
  select, input { min-height: 44px; border: 1px solid var(--border-default); border-radius: 6px; background: var(--surface-elevated); color: var(--text-primary); padding: 0 8px; }
  input[type="range"] { width: 110px; min-height: 0; padding: 0; }
  .state { padding: 32px; border: 1px solid var(--border-default); border-radius: 8px; color: var(--text-muted); background: var(--surface-elevated); }
  .state.error { color: var(--danger); }
  .summary-band { display: grid; grid-template-columns: repeat(4, minmax(130px, 1fr)); gap: 10px; }
  .summary-band div { border: 1px solid var(--border-default); border-radius: 8px; background: var(--surface-elevated); padding: 12px; display: flex; justify-content: space-between; align-items: center; }
  .summary-band strong { font-size: 1.1rem; }
  .summary-band span { color: var(--text-muted); }
  .workspace { display: grid; grid-template-columns: 230px minmax(300px, 420px) minmax(420px, 1fr); gap: 12px; align-items: start; }
  .failure-lanes, .autopsy-list, .detail-pane { border: 1px solid var(--border-default); border-radius: 8px; background: var(--surface-elevated); padding: 12px; }
  .failure-lanes, .autopsy-list { display: flex; flex-direction: column; gap: 7px; }
  .failure-lanes button, .autopsy-list button { width: 100%; min-height: 44px; border: 1px solid var(--border-default); border-radius: 6px; background: var(--surface-default); color: var(--text-primary); padding: 9px; text-align: left; }
  .failure-lanes button { display: flex; justify-content: space-between; align-items: center; }
  .failure-lanes button.active, .autopsy-list button.selected { border-color: var(--accent); }
  .autopsy-list button { display: grid; grid-template-columns: 1fr auto; gap: 5px; }
  .autopsy-list button.degraded { border-color: var(--warning); }
  .run { font-weight: 700; }
  .kind, .followup, .confidence { color: var(--text-muted); font-family: var(--font-mono); }
  .detail-pane { display: flex; flex-direction: column; gap: 12px; }
  .detail-pane header { display: flex; justify-content: space-between; gap: 10px; align-items: flex-start; }
  .detail-pane header p { color: var(--text-muted); font-family: var(--font-mono); margin-top: 3px; }
  .detail-pane header > span { border: 1px solid var(--border-default); border-radius: 6px; padding: 5px 8px; font-family: var(--font-mono); }
  .detail-pane header > span.warning, .state-row.warning { border-color: var(--warning); color: var(--warning); }
  .state-row, .candidate, .followup-card, .evidence { border: 1px solid var(--border-default); border-radius: 6px; background: var(--surface-default); padding: 10px; }
  .state-row { display: flex; justify-content: space-between; gap: 12px; }
  .candidate-stack { display: flex; flex-direction: column; gap: 8px; }
  .candidate div { display: flex; justify-content: space-between; gap: 10px; margin-bottom: 6px; }
  .candidate span { color: var(--text-muted); font-family: var(--font-mono); }
  .candidate p, .followup-card p, .evidence p { color: var(--text-muted); margin-bottom: 7px; }
  .followup-grid { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 5px; margin-bottom: 8px; }
  .followup-grid span { border: 1px solid var(--border-default); border-radius: 6px; padding: 5px; text-align: center; color: var(--text-muted); }
  ul { margin: 0; padding-left: 18px; }
  @media (max-width: 1150px) {
    .workspace { grid-template-columns: 1fr; }
    .summary-band { grid-template-columns: repeat(2, minmax(130px, 1fr)); }
  }
</style>
