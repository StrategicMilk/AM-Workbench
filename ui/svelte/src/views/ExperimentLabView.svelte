<script>
  import { showToast } from '$lib/stores/toast.svelte.js';
  import * as api from '$lib/api.js';
  import { requireEvidence } from '$lib/evidence/evidenceGuard.js';
  import { browserLocationParam } from '$lib/utils/browser.js';

  let { projectId = browserLocationParam('project_id', 'default') } = $props();

  const decisions = ['reject', 'retry', 'promote', 'rollback', 'defer'];

  let experiments = $state([]);
  let selectedExperimentId = $state(null);
  let loading = $state(true);
  let saving = $state(false);
  let error = $state(null);
  let savedMessage = $state('');
  let form = $state(newDraft());

  let selectedExperiment = $derived(experiments.find((item) => item.experiment_id === selectedExperimentId) ?? null);
  let decisionCounts = $derived(
    decisions.reduce((acc, decision) => {
      acc[decision] = experiments.filter((item) => item.decision === decision).length;
      return acc;
    }, {})
  );
  let metricRows = $derived(form.metrics.filter((row) => row.name.trim()));

  function newDraft() {
    return {
      hypothesis: '',
      baseline: { artifact_id: '', artifact_kind: 'method_card', label: '' },
      candidate: { artifact_id: '', artifact_kind: 'method_card', label: '' },
      sample_ref: { sample_id: '', sample_kind: 'benchmark_case', source: '' },
      metrics: [
        { name: 'accuracy', baseline_value: 0, candidate_value: 0, unit: 'ratio', higher_is_better: true },
        { name: 'latency', baseline_value: 0, candidate_value: 0, unit: 'ms', higher_is_better: false },
      ],
      latency_ms: 0,
      cost_usd: 0,
      human_review: { reviewer: '', summary: '' },
      decision: 'defer',
      rationale: '',
    };
  }

  function query(params = {}) {
    const search = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value !== null && value !== undefined && value !== '') search.set(key, value);
    }
    return search.toString() ? `?${search.toString()}` : '';
  }

  async function loadExperiments() {
    loading = true;
    error = null;
    savedMessage = '';
    try {
      experiments = await api.workbenchKernelRequest(`/api/workbench/experiment-lab${query({ project_id: projectId })}`);
      if (!selectedExperimentId && experiments.length > 0) selectedExperimentId = experiments[0].experiment_id;
    } catch (err) {
      error = err.message ?? String(err);
      showToast(`Experiment lab load failed: ${error}`, 'error');
    } finally {
      loading = false;
    }
  }

  function addMetric() {
    form.metrics = [...form.metrics, { name: '', baseline_value: 0, candidate_value: 0, unit: '', higher_is_better: true }];
  }

  function resetDraft() {
    form = newDraft();
    savedMessage = '';
  }

  function buildPayload() {
    const experimentRefs = requireEvidence(
      [form.baseline.artifact_id, form.candidate.artifact_id, form.sample_ref.sample_id],
      'experiment_lab.refs',
    );
    if (experimentRefs.length < 3) {
      throw new Error('Experiment records require baseline, candidate, and sample refs.');
    }
    return {
      project_id: projectId,
      ...form,
      metrics: metricRows.map((row) => ({
        name: row.name,
        baseline_value: Number(row.baseline_value),
        candidate_value: Number(row.candidate_value),
        unit: row.unit,
        higher_is_better: Boolean(row.higher_is_better),
      })),
      latency_ms: Number(form.latency_ms),
      cost_usd: Number(form.cost_usd),
    };
  }

  async function saveExperiment() {
    saving = true;
    error = null;
    savedMessage = '';
    try {
      const record = await api.workbenchKernelRequest('/api/workbench/experiment-lab', {
        method: 'POST',
        body: JSON.stringify(buildPayload()),
      });
      experiments = [record, ...experiments];
      selectedExperimentId = record.experiment_id;
      savedMessage = `Saved ${record.decision} decision`;
      showToast(savedMessage, 'success');
    } catch (err) {
      error = err.message ?? String(err);
      showToast(`Experiment save failed: ${error}`, 'error');
    } finally {
      saving = false;
    }
  }

  $effect(() => {
    void loadExperiments();
  });

  // ---------------------------------------------------------------------------
  // FSA-0048: side-by-side comparison via OpenAI-compatible chat completions
  // ---------------------------------------------------------------------------
  let comparisonModels = $state(['auto', 'auto']);
  let comparisonPrompt = $state('');
  let comparisonResults = $state([null, null]);
  let comparisonRunning = $state(false);
  let comparisonError = $state('');

  async function runComparison() {
    if (!comparisonPrompt.trim() || comparisonRunning) return;
    comparisonRunning = true;
    comparisonError = '';
    try {
      const requests = comparisonModels.map((model) =>
        api.createChatCompletion({
          model,
          messages: [{ role: 'user', content: comparisonPrompt }],
        }),
      );
      comparisonResults = await Promise.all(requests);
    } catch (err) {
      comparisonError = err.message ?? String(err);
    } finally {
      comparisonRunning = false;
    }
  }
</script>

<div class="experiment-lab">
  <header class="lab-header">
    <div>
      <h2>Experiment Lab</h2>
      <p>{projectId}</p>
    </div>
    <div class="toolbar">
      <button type="button" title="Refresh experiments" onclick={loadExperiments}>
        <i class="fas fa-rotate" aria-hidden="true"></i>
      </button>
      <button type="button" title="New draft" onclick={resetDraft}>
        <i class="fas fa-plus" aria-hidden="true"></i>
      </button>
      <button type="button" title="Save experiment" onclick={saveExperiment} disabled={saving}>
        <i class="fas fa-floppy-disk" aria-hidden="true"></i>
      </button>
    </div>
  </header>

  {#if loading}
    <div class="state">Loading experiments.</div>
  {:else if error}
    <div class="state error">{error}</div>
  {:else if experiments.length === 0}
    <div class="state">No controlled comparisons recorded.</div>
  {/if}

  {#if savedMessage}
    <div class="state success">{savedMessage}</div>
  {/if}

  <section
    class="side-by-side-comparison"
    data-testid="fsa0048-side-by-side-comparison"
    aria-label="Side-by-side model comparison"
  >
    <header class="comparison-header">
      <h3>Side-by-side comparison</h3>
      <p>Run the same prompt against two model targets and compare outputs.</p>
    </header>
    <div class="comparison-controls">
      {#each comparisonModels as model, idx (idx)}
        <label class="comparison-model">
          <span>Model {idx + 1}</span>
          <input type="text" bind:value={comparisonModels[idx]} placeholder="auto" />
        </label>
      {/each}
    </div>
    <textarea
      class="comparison-prompt"
      bind:value={comparisonPrompt}
      placeholder="Prompt to send to both models..."
      rows="3"
      aria-label="Comparison prompt"
    ></textarea>
    <div class="comparison-actions">
      <button type="button" onclick={runComparison} disabled={comparisonRunning || !comparisonPrompt.trim()}>
        {comparisonRunning ? 'Running...' : 'Run comparison'}
      </button>
    </div>
    {#if comparisonError}
      <div class="state error" role="alert">{comparisonError}</div>
    {/if}
    <div class="comparison-results">
      {#each comparisonResults as result, idx (idx)}
        <article class="comparison-result">
          <header>{comparisonModels[idx]}</header>
          {#if result}
            <pre>{result.choices?.[0]?.message?.content ?? JSON.stringify(result, null, 2)}</pre>
          {:else}
            <p class="muted">No result yet.</p>
          {/if}
        </article>
      {/each}
    </div>
  </section>

  <section class="decision-strip" aria-label="Decision counts">
    {#each decisions as decision}
      <div class="decision-pill">
        <strong>{decisionCounts[decision] ?? 0}</strong>
        <span>{decision}</span>
      </div>
    {/each}
  </section>

  <main class="workspace">
    <section class="panel form-panel" aria-label="Experiment draft">
      <div class="panel-heading">
        <h3>Draft</h3>
        <span>{form.decision}</span>
      </div>

      <label>
        <span>Hypothesis</span>
        <textarea bind:value={form.hypothesis} rows="3"></textarea>
      </label>

      <div class="comparison-grid">
        <fieldset>
          <legend>Baseline</legend>
          <input bind:value={form.baseline.artifact_id} placeholder="artifact id" />
          <input bind:value={form.baseline.artifact_kind} placeholder="kind" />
          <input bind:value={form.baseline.label} aria-label="Baseline label" placeholder="label" />
        </fieldset>
        <fieldset>
          <legend>Candidate</legend>
          <input bind:value={form.candidate.artifact_id} placeholder="artifact id" />
          <input bind:value={form.candidate.artifact_kind} placeholder="kind" />
          <input bind:value={form.candidate.label} aria-label="Candidate label" placeholder="label" />
        </fieldset>
      </div>

      <fieldset>
        <legend>Sample</legend>
        <div class="sample-grid">
          <input bind:value={form.sample_ref.sample_id} placeholder="sample id" />
          <input bind:value={form.sample_ref.sample_kind} placeholder="sample kind" />
          <input bind:value={form.sample_ref.source} placeholder="source" />
        </div>
      </fieldset>

      <div class="metric-header">
        <h4>Metrics</h4>
        <button type="button" title="Add metric" onclick={addMetric}>
          <i class="fas fa-plus" aria-hidden="true"></i>
        </button>
      </div>
      <div class="metrics-grid">
        <span>Name</span>
        <span>Baseline</span>
        <span>Candidate</span>
        <span>Unit</span>
        <span>Higher</span>
        {#each form.metrics as metric}
          <input bind:value={metric.name} placeholder="metric" />
          <input type="number" step="0.001" bind:value={metric.baseline_value} />
          <input type="number" step="0.001" bind:value={metric.candidate_value} />
          <input bind:value={metric.unit} placeholder="unit" />
          <input type="checkbox" bind:checked={metric.higher_is_better} aria-label="higher is better" />
        {/each}
      </div>

      <div class="numbers-grid">
        <label>
          <span>Latency ms</span>
          <input type="number" step="0.1" bind:value={form.latency_ms} />
        </label>
        <label>
          <span>Cost USD</span>
          <input type="number" step="0.001" bind:value={form.cost_usd} />
        </label>
      </div>

      <label>
        <span>Human review</span>
        <textarea bind:value={form.human_review.summary} rows="3"></textarea>
      </label>

      <div class="decision-control" aria-label="Decision">
        {#each decisions as decision}
          <button
            type="button"
            class:active={form.decision === decision}
            aria-pressed={form.decision === decision}
            onclick={() => { form.decision = decision; }}
          >
            {decision}
          </button>
        {/each}
      </div>

      <label>
        <span>Rationale</span>
        <textarea bind:value={form.rationale} rows="3"></textarea>
      </label>
    </section>

    <section class="panel list-panel" aria-label="Experiment records">
      <div class="panel-heading">
        <h3>Records</h3>
        <span>{experiments.length}</span>
      </div>
      <div class="record-list">
        {#each experiments as experiment (experiment.experiment_id)}
          <button
            type="button"
            class:selected={selectedExperimentId === experiment.experiment_id}
            aria-pressed={selectedExperimentId === experiment.experiment_id}
            onclick={() => { selectedExperimentId = experiment.experiment_id; }}
          >
            <strong>{experiment.hypothesis}</strong>
            <small>{experiment.decision} · {experiment.sample_ref.sample_kind}</small>
            <span>{experiment.rationale}</span>
          </button>
        {/each}
      </div>
    </section>

    <aside class="panel detail-panel" aria-label="Selected experiment">
      <div class="panel-heading">
        <h3>Comparison</h3>
        <span>{selectedExperiment?.decision ?? 'none'}</span>
      </div>
      {#if selectedExperiment}
        <div class="compare-table">
          <span></span>
          <strong>Baseline</strong>
          <strong>Candidate</strong>
          <span>Artifact</span>
          <span>{selectedExperiment.baseline.label || selectedExperiment.baseline.artifact_id}</span>
          <span>{selectedExperiment.candidate.label || selectedExperiment.candidate.artifact_id}</span>
          <span>Kind</span>
          <span>{selectedExperiment.baseline.artifact_kind}</span>
          <span>{selectedExperiment.candidate.artifact_kind}</span>
        </div>
        <table>
          <thead>
            <tr><th>Metric</th><th>Baseline</th><th>Candidate</th><th>Delta</th></tr>
          </thead>
          <tbody>
            {#each selectedExperiment.metrics as metric}
              <tr>
                <td>{metric.name}</td>
                <td>{metric.baseline_value}</td>
                <td>{metric.candidate_value}</td>
                <td>{metric.delta}</td>
              </tr>
            {/each}
          </tbody>
        </table>
        <div class="detail-copy">
          <h4>Review</h4>
          <p>{selectedExperiment.human_review.summary}</p>
          <h4>Rationale</h4>
          <p>{selectedExperiment.rationale}</p>
        </div>
      {:else}
        <div class="state">Select or save an experiment.</div>
      {/if}
    </aside>
  </main>
</div>

<style>
  .experiment-lab { padding: 18px; max-width: 1500px; display: flex; flex-direction: column; gap: 12px; color: var(--text-primary); }
  .lab-header { display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; flex-wrap: wrap; }
  h2, h3, h4, p { margin: 0; }
  h2 { font-size: 1.25rem; }
  h3 { font-size: 0.95rem; }
  h4 { font-size: 0.82rem; }
  p, span, input, textarea, button, td, th, small { font-size: 0.82rem; }
  .lab-header p { margin-top: 3px; color: var(--text-muted); font-family: var(--font-mono); }
  .toolbar { display: flex; gap: 6px; }
  .toolbar button, .metric-header button { min-width: 44px; min-height: 44px; display: inline-flex; align-items: center; justify-content: center; border: 1px solid var(--border-default); border-radius: 6px; background: var(--surface-elevated); color: var(--text-primary); }
  .state { padding: 14px; border: 1px solid var(--border-default); border-radius: 8px; background: var(--surface-elevated); color: var(--text-muted); }
  .error { color: var(--danger); }
  .success { color: var(--success); }
  .decision-strip { display: grid; grid-template-columns: repeat(5, minmax(110px, 1fr)); gap: 8px; }
  .decision-pill { border: 1px solid var(--border-default); border-radius: 8px; background: var(--surface-elevated); padding: 10px; display: flex; align-items: center; justify-content: space-between; }
  .workspace { display: grid; grid-template-columns: minmax(360px, 1.05fr) minmax(260px, 0.7fr) minmax(320px, 0.85fr); gap: 12px; align-items: start; }
  .panel { border: 1px solid var(--border-default); border-radius: 8px; background: var(--surface-elevated); padding: 12px; display: flex; flex-direction: column; gap: 12px; min-width: 0; }
  .panel-heading, .metric-header { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
  .panel-heading span { color: var(--text-muted); font-family: var(--font-mono); }
  label, fieldset { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
  fieldset { border: 1px solid var(--border-default); border-radius: 8px; padding: 10px; }
  legend, label span { color: var(--text-muted); font-size: 0.76rem; }
  input, textarea { min-width: 0; width: 100%; border: 1px solid var(--border-default); border-radius: 6px; background: var(--surface-default); color: var(--text-primary); padding: 8px; }
  textarea { resize: vertical; }
  .comparison-grid, .numbers-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
  .sample-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }
  .metrics-grid { display: grid; grid-template-columns: minmax(90px, 1fr) 86px 86px 74px 52px; gap: 6px; align-items: center; }
  .metrics-grid > span { color: var(--text-muted); font-size: 0.74rem; }
  .metrics-grid input[type='checkbox'] { width: 18px; height: 18px; justify-self: center; }
  .decision-control { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 6px; }
  .decision-control button, .record-list button { min-height: 44px; border: 1px solid var(--border-default); border-radius: 6px; background: var(--surface-default); color: var(--text-primary); padding: 8px; }
  .decision-control button.active, .record-list button.selected { border-color: var(--accent); background: var(--surface-selected); }
  .record-list { display: flex; flex-direction: column; gap: 8px; }
  .record-list button { text-align: left; display: flex; flex-direction: column; gap: 4px; }
  .record-list small { color: var(--text-muted); font-family: var(--font-mono); }
  .compare-table { display: grid; grid-template-columns: 86px repeat(2, minmax(0, 1fr)); gap: 6px; align-items: center; }
  .compare-table span, .compare-table strong { overflow-wrap: anywhere; }
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; border-bottom: 1px solid var(--border-default); padding: 6px; }
  .detail-copy { display: flex; flex-direction: column; gap: 6px; }
  @media (max-width: 1180px) {
    .workspace { grid-template-columns: 1fr; }
    .decision-strip { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  }
  @media (max-width: 720px) {
    .comparison-grid, .numbers-grid, .sample-grid, .decision-control { grid-template-columns: 1fr; }
    .metrics-grid { grid-template-columns: 1fr; }
    .metrics-grid > span { display: none; }
  }
</style>
