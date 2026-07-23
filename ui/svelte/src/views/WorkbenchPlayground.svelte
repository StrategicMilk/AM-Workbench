<script lang="ts">
  import ExperimentWinnerPanel from '$components/workbench/playground/ExperimentWinnerPanel.svelte';
  import { appState } from '$lib/stores/app.svelte.js';
  import { workbenchKernelRequest } from '$lib/api.js';
  import { readWorkbenchJourneyState } from '$lib/workbench/journey_router.ts';

  type EvalScore = {
    metric_name: string;
    value: number;
    threshold: number;
    passed: boolean;
    unit?: string;
  };

  type EvalResult = {
    eval_id: string;
    kind: string;
    run_id: string;
    asset_id: string;
    asset_revision: string;
    scores: EvalScore[];
    captured_at_utc: string;
    notes?: string;
  };

  type ReplayScaffold = {
    trace_id: string;
    run_id: string;
    prompt_text: string;
    agent_edits: string[];
    tool_overrides: string[];
    model_overrides: string[];
    captured_at_utc: string;
  };

  type PlaygroundExperiment = {
    experiment_id: string;
    source_trace_id: string;
    source_run_id: string;
    asset_id: string;
    asset_revision: string;
    prompt_text: string;
    agent_edits: string[];
    tool_overrides: string[];
    model_overrides: string[];
    created_at_utc: string;
    score: number;
    notes: string;
  };

  const initialJourneyState = readWorkbenchJourneyState(window.location.search, appState.currentProjectId || 'default');
  let projectId = $state(initialJourneyState.projectId);
  let runId = $state(initialJourneyState.runId ?? '');
  let traceId = $state(initialJourneyState.traceId ?? initialJourneyState.evidenceTraceId ?? '');
  let assetId = $state(initialJourneyState.assetId ?? '');
  let assetRevision = $state(initialJourneyState.assetRevision ?? '');
  let promptText = $state(initialJourneyState.promptText ?? '');
  let agentEditsText = $state('');
  let toolOverridesText = $state('');
  let modelOverridesText = $state('');
  let evalResult = $state<EvalResult | null>(null);
  let scaffold = $state<ReplayScaffold | null>(null);
  let experiments = $state<PlaygroundExperiment[]>([]);
  let lastError = $state<{ message: string; reason: string } | null>(null);
  let busy = $state(false);
  let loadingExperiments = $state(false);

  let canDispatch = $derived(
    runId.trim().length > 0 &&
      traceId.trim().length > 0 &&
      assetId.trim().length > 0 &&
      assetRevision.trim().length > 0 &&
      !busy
  );
  let experimentCount = $derived(experiments.length);
  let winningExperiment = $derived(
    [...experiments].sort((a, b) => b.score - a.score)[0] ?? null
  );

  function splitOverrides(value: string): string[] {
    return value
      .split(String.fromCharCode(10))
      .map((row) => row.trim())
      .filter(Boolean);
  }

  function payload() {
    return {
      run_id: runId.trim(),
      trace_id: traceId.trim(),
      asset_id: assetId.trim(),
      asset_revision: assetRevision.trim(),
      prompt_text: promptText,
      agent_edits: splitOverrides(agentEditsText),
      tool_overrides: splitOverrides(toolOverridesText),
      model_overrides: splitOverrides(modelOverridesText),
    };
  }

  async function parseError(error: unknown, fallback: string): Promise<{ message: string; reason: string }> {
    const body = error as { message?: string; reason?: string };
    return {
      message: body.message ?? fallback,
      reason: body.reason ?? 'request_failed',
    };
  }

  async function postPlaygroundJson(url: string) {
    return workbenchKernelRequest(url, {
      method: 'POST',
      headers: { 'X-Requested-With': 'XMLHttpRequest' },
      body: JSON.stringify(payload()),
    });
  }

  async function dispatchTraceToEval() {
    if (!canDispatch) return;
    busy = true;
    lastError = null;
    try {
      const body = await postPlaygroundJson('/api/workbench/playground/trace-to-eval');
      if (!body) return;
      evalResult = body.eval_result ?? null;
      scaffold = body.scaffold ?? null;
      if (scaffold?.prompt_text && !promptText.trim()) {
        promptText = scaffold.prompt_text;
      }
    } catch (err) {
      lastError = await parseError(err, 'trace-to-eval failed');
    } finally {
      busy = false;
    }
  }

  async function runExperiment() {
    if (!canDispatch) return;
    busy = true;
    lastError = null;
    try {
      const body = await postPlaygroundJson('/api/workbench/playground/run');
      if (!body) return;
      await refreshExperiments();
    } catch (err) {
      lastError = await parseError(err, 'experiment run failed');
    } finally {
      busy = false;
    }
  }

  async function refreshExperiments() {
    loadingExperiments = true;
    try {
      const body = await workbenchKernelRequest('/api/workbench/playground/experiments');
      experiments = body.experiments ?? [];
    } catch (err) {
      lastError = await parseError(err, 'experiments refresh failed');
    } finally {
      loadingExperiments = false;
    }
  }

  $effect(() => {
    void refreshExperiments();
  });
</script>

<div class="workbench-playground">
  <header class="view-header">
    <h1>Workbench Playground</h1>
    <p>Promote real spine traces into eval cases, adjust replay inputs, and save comparable scratch experiments.</p>
  </header>

  <section class="workspace-band">
    <form class="dispatch-panel" onsubmit={(event) => event.preventDefault()}>
      <div class="field-grid">
        <label>
          Run ID
          <input bind:value={runId} autocomplete="off" placeholder="run-1" />
        </label>
        <label>
          Trace ID
          <input bind:value={traceId} autocomplete="off" placeholder="trace-1" />
        </label>
        <label>
          Asset ID
          <input bind:value={assetId} autocomplete="off" placeholder="asset-1" />
        </label>
        <label>
          Asset Revision
          <input bind:value={assetRevision} autocomplete="off" placeholder="v1" />
        </label>
      </div>

      <label class="wide-field">
        Prompt text
        <textarea bind:value={promptText} rows="4" placeholder="Prompt or replay input"></textarea>
      </label>

      <div class="override-grid">
        <label>
          Agent edits
          <textarea bind:value={agentEditsText} rows="5" placeholder="One edit per line"></textarea>
        </label>
        <label>
          Tool overrides
          <textarea bind:value={toolOverridesText} rows="5" placeholder="One override per line"></textarea>
        </label>
        <label>
          Model overrides
          <textarea bind:value={modelOverridesText} rows="5" placeholder="One setting per line"></textarea>
        </label>
      </div>

      <div class="action-row">
        <button type="button" class="primary" disabled={!canDispatch} onclick={dispatchTraceToEval}>
          Dispatch trace to eval
        </button>
        <button type="button" disabled={!canDispatch} onclick={runExperiment}>
          Run experiment
        </button>
        <button type="button" disabled={loadingExperiments} onclick={refreshExperiments}>
          Refresh
        </button>
      </div>
    </form>

    <aside class="result-panel">
      {#if lastError}
        <div class="error" role="alert">
          Could not complete request: {lastError.message} (reason: {lastError.reason}).
        </div>
      {/if}

      {#if evalResult}
        <section>
          <h2>Derived eval case</h2>
          <dl>
            <div><dt>Eval ID</dt><dd>{evalResult.eval_id}</dd></div>
            <div><dt>Kind</dt><dd>{evalResult.kind}</dd></div>
            <div><dt>Run</dt><dd>{evalResult.run_id}</dd></div>
            <div><dt>Asset</dt><dd>{evalResult.asset_id}@{evalResult.asset_revision}</dd></div>
            <div><dt>Notes</dt><dd>{evalResult.notes ?? 'none'}</dd></div>
          </dl>
        </section>
      {:else}
        <section>
          <h2>Derived eval case</h2>
          <p class="empty-state">No eval case dispatched in this session.</p>
        </section>
      {/if}

      {#if scaffold}
        <section>
          <h2>Replay scaffold</h2>
          <dl>
            <div><dt>Trace</dt><dd>{scaffold.trace_id}</dd></div>
            <div><dt>Run</dt><dd>{scaffold.run_id}</dd></div>
            <div><dt>Captured</dt><dd>{scaffold.captured_at_utc}</dd></div>
          </dl>
        </section>
      {/if}
    </aside>
  </section>

  <section class="experiments-band">
    <div class="section-heading">
      <h2>Experiments ({experimentCount})</h2>
      {#if loadingExperiments}<span>Refreshing</span>{/if}
    </div>

    <ExperimentWinnerPanel experiment={winningExperiment} {projectId} />

    {#if experiments.length > 0}
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Experiment</th>
              <th>Trace</th>
              <th>Asset</th>
              <th>Score</th>
              <th>Created</th>
            </tr>
          </thead>
          <tbody>
            {#each experiments as exp (exp.experiment_id)}
              <tr>
                <td>{exp.experiment_id}</td>
                <td>{exp.source_trace_id}</td>
                <td>{exp.asset_id}@{exp.asset_revision}</td>
                <td>{exp.score}</td>
                <td>{exp.created_at_utc}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {:else}
      <p class="empty-state">No scratch experiments are currently held by the playground.</p>
    {/if}
  </section>
</div>

<style>
  .workbench-playground {
    display: flex;
    flex-direction: column;
    gap: 1.25rem;
    color: var(--color-text, #20242a);
  }

  .view-header h1 {
    margin: 0 0 0.25rem;
    font-size: 1.75rem;
    letter-spacing: 0;
  }

  .view-header p,
  .empty-state {
    margin: 0;
    color: var(--color-text-muted, #68707c);
  }

  .workspace-band {
    display: grid;
    grid-template-columns: minmax(0, 1.4fr) minmax(18rem, 0.8fr);
    gap: 1rem;
    align-items: start;
  }

  .dispatch-panel,
  .result-panel,
  .experiments-band {
    border: 1px solid var(--color-border, #d8dde3);
    border-radius: 8px;
    background: var(--color-surface, #fff);
    padding: 1rem;
  }

  .field-grid,
  .override-grid {
    display: grid;
    gap: 0.75rem;
  }

  .field-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .override-grid {
    grid-template-columns: repeat(3, minmax(0, 1fr));
    margin-top: 0.75rem;
  }

  label {
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
    font-size: 0.84rem;
    font-weight: 600;
  }

  input,
  textarea {
    min-width: 0;
    border: 1px solid var(--color-border, #c9d1da);
    border-radius: 6px;
    padding: 0.55rem 0.65rem;
    font: inherit;
    color: inherit;
    background: #fff;
  }

  .wide-field {
    margin-top: 0.75rem;
  }

  .action-row {
    display: flex;
    flex-wrap: wrap;
    gap: 0.6rem;
    margin-top: 0.9rem;
  }

  button {
    border: 1px solid var(--color-border, #c9d1da);
    border-radius: 6px;
    padding: 0.55rem 0.8rem;
    font: inherit;
    font-weight: 600;
    background: #f6f8fa;
    color: #20242a;
    cursor: pointer;
  }

  button.primary {
    border-color: #176d6b;
    background: #176d6b;
    color: #fff;
  }

  button:disabled {
    cursor: not-allowed;
    opacity: 0.55;
  }

  .result-panel {
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }

  .result-panel h2,
  .experiments-band h2 {
    margin: 0 0 0.6rem;
    font-size: 1rem;
    letter-spacing: 0;
  }

  dl {
    display: grid;
    gap: 0.4rem;
    margin: 0;
  }

  dl div {
    display: grid;
    grid-template-columns: 5rem minmax(0, 1fr);
    gap: 0.5rem;
  }

  dt {
    color: var(--color-text-muted, #68707c);
    font-weight: 600;
  }

  dd {
    margin: 0;
    overflow-wrap: anywhere;
  }

  .error {
    border: 1px solid #b42318;
    border-radius: 6px;
    background: #fff4f2;
    color: #8a1f14;
    padding: 0.7rem;
  }

  .section-heading {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 1rem;
  }

  .table-wrap {
    margin-top: 0.9rem;
    overflow-x: auto;
  }

  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.9rem;
  }

  th,
  td {
    border-bottom: 1px solid var(--color-border, #e2e6eb);
    padding: 0.55rem;
    text-align: left;
    vertical-align: top;
  }

  td {
    overflow-wrap: anywhere;
  }

  @media (max-width: 900px) {
    .workspace-band,
    .field-grid,
    .override-grid {
      grid-template-columns: 1fr;
    }
  }
</style>
