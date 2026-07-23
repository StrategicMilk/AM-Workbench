<script>
  /**
   * Training management view — control training runs, configure hyperparameters,
   * review history, and manage idle/kaizen training.
   */
  import * as api from '$lib/api.js';
  import * as fmt from '$lib/utils/format.js';
  import { showToast } from '$lib/stores/toast.svelte.js';
  import HelpPopover from '$lib/components/help/HelpPopover.svelte';
  import HelpTooltip from '$lib/components/help/HelpTooltip.svelte';
  import { requireEvidence } from '$lib/evidence/evidenceGuard.js';

  // -- State -------------------------------------------------------------------

  let status = $state(null);
  let history = $state([]);
  let idleStats = $state(null);
  let loading = $state(true);
  let actionPending = $state(false);
  let expandedRunId = $state(null);

  const emptyTrainingConfig = {
    skill: '',
    learning_rate: null,
    epochs: null,
    batch_size: null,
    warmup_steps: null,
    max_seq_len: null,
  };

  let config = $state({ ...emptyTrainingConfig });

  let syntheticConfig = $state({
    num_samples: 100,
    task_type: 'general',
  });

  let showSyntheticForm = $state(false);
  let showConfigForm = $state(false);

  // -- Derived -----------------------------------------------------------------

  let isRunning = $derived(status?.state === 'running');
  let isPaused = $derived(status?.state === 'paused');
  let isIdle = $derived(status?.state === 'idle' || status?.state == null);

  let statusColor = $derived(
    isRunning ? 'success' : isPaused ? 'warning' : 'muted'
  );

  let statusLabel = $derived(
    isRunning ? 'Running' : isPaused ? 'Paused' : 'Idle'
  );

  let progressPct = $derived(
    status?.progress != null ? Math.min(100, Math.max(0, status.progress)) : 0
  );

  function coerceNumber(value, min, max) {
    if (value == null || value === '') return null;
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) return null;
    return Math.max(min, Math.min(max, parsed));
  }

  function normalizeTrainingConfig(raw = {}) {
    const epochs = coerceNumber(raw.epochs, 1, 100);
    const batchSize = coerceNumber(raw.batch_size, 1, 256);
    const warmupSteps = coerceNumber(raw.warmup_steps, 0, 10000);
    const maxSeqLen = coerceNumber(raw.max_seq_len, 128, 131072);
    return {
      skill: String(raw.skill ?? '').trim(),
      learning_rate: coerceNumber(raw.learning_rate, 0.000001, 0.1),
      epochs: epochs == null ? null : Math.round(epochs),
      batch_size: batchSize == null ? null : Math.round(batchSize),
      warmup_steps: warmupSteps == null ? null : Math.round(warmupSteps),
      max_seq_len: maxSeqLen == null ? null : Math.round(maxSeqLen),
    };
  }

  function missingTrainingConfigFields(normalizedConfig) {
    return ['skill', 'learning_rate', 'epochs', 'batch_size', 'warmup_steps', 'max_seq_len']
      .filter((key) => normalizedConfig[key] == null || normalizedConfig[key] === '');
  }

  function normalizeTrainingStatus(raw = null) {
    if (!raw) return null;
    const currentJob = raw.current_job ?? raw.job ?? null;
    const running = raw.is_training === true || currentJob?.status === 'running';
    const paused = currentJob?.status === 'paused' || raw.phase === 'paused';
    const progress = currentJob?.progress != null
      ? Number(currentJob.progress) * 100
      : raw.progress != null
        ? Number(raw.progress)
        : 0;
    return {
      ...raw,
      state: running ? 'running' : paused ? 'paused' : raw.phase ?? raw.state ?? 'idle',
      progress: Number.isFinite(progress) ? progress : 0,
      current_epoch: raw.current_epoch ?? currentJob?.current_epoch,
      total_epochs: raw.total_epochs ?? config.epochs,
      current_step: raw.current_step ?? currentJob?.current_step,
      total_steps: raw.total_steps ?? currentJob?.total_steps,
      current_loss: raw.current_loss ?? currentJob?.current_loss,
      eta_ms: raw.eta_ms ?? currentJob?.eta_ms,
      last_run: raw.last_run?.updated_at_utc ?? raw.last_run?.started_at ?? raw.last_run,
      last_run_result: raw.last_run?.status ?? raw.last_run_result,
    };
  }

  function normalizeTrainingHistory(raw = {}) {
    const rows = raw.jobs ?? raw.agents ?? raw.runs ?? raw.history ?? raw;
    return Array.isArray(rows) ? rows : [];
  }

  function validateTrainingHistoryRefs(rows) {
    const refs = rows.map((run) => run.id ?? run.run_id).filter(Boolean);
    if (refs.length > 0) {
      requireEvidence(refs, 'training.history_run_refs');
    }
  }

  // -- Data loading ------------------------------------------------------------

  async function loadAll() {
    loading = true;
    try {
      const [s, h, i, prefs] = await Promise.all([
        api.getTrainingStatus().catch(() => null),
        api.getTrainingHistory().catch(() => ({ agents: [] })),
        api.getIdleTrainingStats().catch(() => null),
        api.getPreferences().catch(() => ({ preferences: {} })),
      ]);
      status = normalizeTrainingStatus(s);
      history = normalizeTrainingHistory(h);
      validateTrainingHistoryRefs(history);
      idleStats = i;
      config = normalizeTrainingConfig(prefs?.preferences?.trainingConfig ?? config);
    } catch (err) {
      showToast(`Failed to load training data: ${err.message}`, 'error');
    } finally {
      loading = false;
    }
  }

  $effect(() => { loadAll(); });

  // -- Actions -----------------------------------------------------------------

  async function handleStart() {
    const normalizedConfig = normalizeTrainingConfig(config);
    const missingFields = missingTrainingConfigFields(normalizedConfig);
    if (missingFields.length > 0) {
      showToast(`Training configuration is incomplete: ${missingFields.join(', ')}`, 'error');
      return;
    }
    actionPending = true;
    try {
      await api.setPreferences({ trainingConfig: normalizedConfig }).catch(() => null);
      await api.startTraining(normalizedConfig);
      config = normalizedConfig;
      showToast('Training started', 'success');
      await loadAll();
    } catch (err) {
      showToast(`Failed to start training: ${err.message}`, 'error');
    } finally {
      actionPending = false;
    }
  }

  async function handlePause() {
    actionPending = true;
    try {
      await api.pauseTraining();
      showToast('Training paused', 'info');
      await loadAll();
    } catch (err) {
      showToast(`Failed to pause: ${err.message}`, 'error');
    } finally {
      actionPending = false;
    }
  }

  async function handleStop() {
    if (!confirm('Stop the current training run? Progress after the last checkpoint may be lost.')) return;
    actionPending = true;
    try {
      await api.stopTraining();
      showToast('Training stopped', 'info');
      await loadAll();
    } catch (err) {
      showToast(`Failed to stop: ${err.message}`, 'error');
    } finally {
      actionPending = false;
    }
  }

  async function handleDryRun() {
    const normalizedConfig = normalizeTrainingConfig(config);
    const missingFields = missingTrainingConfigFields(normalizedConfig);
    if (missingFields.length > 0) {
      showToast(`Training dry run requires configuration: ${missingFields.join(', ')}`, 'error');
      return;
    }
    actionPending = true;
    try {
      await api.setPreferences({ trainingConfig: normalizedConfig }).catch(() => null);
      const result = await api.dryRunTraining(normalizedConfig);
      config = normalizedConfig;
      showToast(
        `Dry run complete — estimated ${fmt.duration(result?.estimated_duration_ms)} for ${result?.estimated_steps ?? '?'} steps`,
        'info'
      );
    } catch (err) {
      showToast(`Dry run failed: ${err.message}`, 'error');
    } finally {
      actionPending = false;
    }
  }

  async function handleGenerateSynthetic() {
    actionPending = true;
    try {
      const result = await api.generateSyntheticData(syntheticConfig);
      showToast(
        `Generated ${result?.count ?? syntheticConfig.num_samples} synthetic samples`,
        'success'
      );
      showSyntheticForm = false;
      await loadAll();
    } catch (err) {
      showToast(`Synthetic generation failed: ${err.message}`, 'error');
    } finally {
      actionPending = false;
    }
  }

  function toggleRunDetails(runId) {
    expandedRunId = expandedRunId === runId ? null : runId;
  }

  function runStatusColor(state) {
    if (state === 'completed') return 'success';
    if (state === 'failed') return 'danger';
    if (state === 'running') return 'primary';
    return 'muted';
  }
</script>

<div class="training-view">
  <div class="view-header">
    <h2>
      <i class="fas fa-graduation-cap"></i>
      Training
    </h2>
    <HelpPopover
      title="Training"
      body="Training records collected from completed runs in this project. Each record captures the agent type, task description, output quality score, and the inference parameters used — forming the dataset for future fine-tuning or Thompson Sampling weight updates. Records with quality score below the project threshold are flagged and excluded from the active training corpus automatically. Use the filter controls to review flagged records before manual promotion or deletion."
      severity="info"
    />
    <div class="header-actions">
      <button
        class="btn btn-secondary btn-sm"
        onclick={loadAll}
        disabled={loading}
        aria-label="Refresh training data"
      >
        <i class="fas fa-sync-alt" class:fa-spin={loading}></i>
        Refresh
      </button>
    </div>
  </div>

  {#if loading}
    <div class="loading-state" role="status" aria-live="polite">
      <i class="fas fa-spinner fa-spin"></i>
      Loading training status...
    </div>
  {:else}
    <div class="training-layout">
      <!-- Left column: status + controls -->
      <div class="training-main">

        <!-- Status card -->
        <section class="card status-card" aria-label="Training status">
          <div class="card-header">
            <h3><i class="fas fa-circle-notch"></i> Status</h3>
            <span class="status-badge status-{statusColor}" role="status" aria-label="Training state: {statusLabel}" aria-live="polite">
              {statusLabel}
            </span>
          </div>

          {#if isRunning}
            <div class="progress-block">
              <div class="progress-meta">
                <span>Epoch {status?.current_epoch ?? '?'} / {status?.total_epochs ?? config.epochs}</span>
                <span>{fmt.percent(progressPct, 0)}</span>
              </div>
              <div class="progress-bar" role="progressbar" aria-valuenow={progressPct} aria-valuemin="0" aria-valuemax="100">
                <div class="progress-fill" style="width: {progressPct}%"></div>
              </div>
              <div class="progress-detail">
                <span>Step {fmt.integer(status?.current_step ?? 0)} / {fmt.integer(status?.total_steps ?? 0)}</span>
                <span>Loss: {status?.current_loss != null ? fmt.decimal(status.current_loss, 4) : '—'}</span>
                <span>ETA: {status?.eta_ms != null ? fmt.duration(status.eta_ms) : '—'}</span>
              </div>
            </div>
          {:else if status?.last_run}
            <p class="status-last-run">
              Last run: {fmt.datetime(status.last_run)} — {status.last_run_result ?? 'unknown result'}
            </p>
          {:else}
            <p class="status-empty">No training runs yet.</p>
          {/if}

          <!-- Controls -->
          <div class="controls-row" role="group" aria-label="Training controls">
            {#if isRunning}
              <button
                class="btn btn-warning"
                onclick={handlePause}
                disabled={actionPending}
                aria-label="Pause training"
              >
                <i class="fas fa-pause"></i> Pause
              </button>
              <button
                class="btn btn-danger"
                onclick={handleStop}
                disabled={actionPending}
                aria-label="Stop training"
              >
                <i class="fas fa-stop"></i> Stop
              </button>
            {:else if isPaused}
              <button
                class="btn btn-primary"
                onclick={handleStart}
                disabled={actionPending}
                aria-label="Resume training"
              >
                <i class="fas fa-play"></i> Resume
              </button>
              <button
                class="btn btn-danger"
                onclick={handleStop}
                disabled={actionPending}
                aria-label="Stop training"
              >
                <i class="fas fa-stop"></i> Stop
              </button>
            {:else}
              <button
                class="btn btn-primary"
                onclick={handleStart}
                disabled={actionPending}
                aria-label="Start training"
              >
                <i class="fas fa-play"></i> Start
              </button>
              <button
                class="btn btn-secondary"
                onclick={handleDryRun}
                disabled={actionPending}
                aria-label="Dry run training"
              >
                <i class="fas fa-flask"></i> Dry Run
              </button>
            {/if}
          </div>
        </section>

        <!-- Training config -->
        <section class="card config-card" aria-label="Training configuration">
          <div class="card-header">
            <h3><i class="fas fa-sliders-h"></i> Configuration</h3>
            <button
              class="btn-icon"
              onclick={() => { showConfigForm = !showConfigForm; }}
              aria-expanded={showConfigForm}
              aria-label="Toggle configuration form"
            >
              <i class="fas" class:fa-chevron-down={!showConfigForm} class:fa-chevron-up={showConfigForm}></i>
            </button>
          </div>

          {#if showConfigForm}
            <form class="config-form" onsubmit={(e) => { e.preventDefault(); handleStart(); }} aria-label="Training hyperparameters">
              <div class="form-grid">
                <label class="form-group">
                  <span class="form-label">Skill</span>
                  <input
                    class="input"
                    bind:value={config.skill}
                    aria-label="Training skill"
                  />
                </label>
                <label class="form-group">
                  <span class="form-label">Learning Rate</span>
                  <input
                    type="number"
                    class="input"
                    bind:value={config.learning_rate}
                    min="0.000001"
                    max="0.1"
                    step="0.000001"
                    aria-label="Learning rate"
                  />
                </label>
                <label class="form-group">
                  <span class="form-label">Epochs</span>
                  <input
                    type="number"
                    class="input"
                    bind:value={config.epochs}
                    min="1"
                    max="100"
                    step="1"
                    aria-label="Number of epochs"
                  />
                </label>
                <label class="form-group">
                  <span class="form-label">Batch Size</span>
                  <input
                    type="number"
                    class="input"
                    bind:value={config.batch_size}
                    min="1"
                    max="256"
                    step="1"
                    aria-label="Batch size"
                  />
                </label>
                <label class="form-group">
                  <span class="form-label">Warmup Steps</span>
                  <input
                    type="number"
                    class="input"
                    bind:value={config.warmup_steps}
                    min="0"
                    max="10000"
                    step="10"
                    aria-label="Warmup steps"
                  />
                </label>
                <label class="form-group">
                  <span class="form-label">Max Seq Length</span>
                  <input
                    type="number"
                    class="input"
                    bind:value={config.max_seq_len}
                    min="128"
                    max="131072"
                    step="128"
                    aria-label="Maximum sequence length"
                  />
                </label>
              </div>
            </form>
          {:else}
            <dl class="config-summary">
              <dt>LR</dt><dd>{config.learning_rate}</dd>
              <dt>Skill</dt><dd>{config.skill}</dd>
              <dt>Epochs</dt><dd>{config.epochs}</dd>
              <dt>Batch</dt><dd>{config.batch_size}</dd>
              <dt>Warmup</dt><dd>{config.warmup_steps}</dd>
              <dt>Max Seq</dt><dd>{config.max_seq_len == null ? '—' : fmt.integer(config.max_seq_len)}</dd>
            </dl>
          {/if}
        </section>

        <!-- Synthetic data generation -->
        <section class="card synthetic-card" aria-label="Synthetic data generation">
          <div class="card-header">
            <h3><i class="fas fa-robot"></i> Synthetic Data</h3>
            <button
              class="btn btn-secondary btn-sm"
              onclick={() => { showSyntheticForm = !showSyntheticForm; }}
              aria-expanded={showSyntheticForm}
              aria-label="Toggle synthetic data form"
            >
              <i class="fas fa-magic"></i> Generate
            </button>
          </div>

          {#if showSyntheticForm}
            <form
              class="synthetic-form"
              onsubmit={(e) => { e.preventDefault(); handleGenerateSynthetic(); }}
              aria-label="Synthetic data generation options"
            >
              <div class="form-grid">
                <label class="form-group">
                  <span class="form-label">Sample Count</span>
                  <input
                    type="number"
                    class="input"
                    bind:value={syntheticConfig.num_samples}
                    min="10"
                    max="10000"
                    step="10"
                    aria-label="Number of synthetic samples to generate"
                  />
                </label>
                <label class="form-group">
                  <span class="form-label">Task Type</span>
                  <select class="input" bind:value={syntheticConfig.task_type} aria-label="Synthetic task type">
                    <option value="general">General</option>
                    <option value="coding">Coding</option>
                    <option value="reasoning">Reasoning</option>
                    <option value="conversation">Conversation</option>
                    <option value="analysis">Analysis</option>
                  </select>
                </label>
              </div>
              <button
                type="submit"
                class="btn btn-primary"
                disabled={actionPending}
                aria-label="Generate synthetic training data"
              >
                <i class="fas fa-play"></i>
                Generate {syntheticConfig.num_samples} Samples
              </button>
            </form>
          {/if}
        </section>

        <!-- History table -->
        <section class="card history-card" aria-label="Training run history">
          <div class="card-header">
            <h3><i class="fas fa-history"></i> History</h3>
            <span class="badge">{history.length}</span>
          </div>

          {#if history.length === 0}
            <div class="empty-state">
              <i class="fas fa-inbox"></i>
              <p>No training runs yet.</p>
            </div>
          {:else}
            <div class="history-table-wrap" role="region" aria-label="Training run history table">
              <table class="history-table" aria-label="Training history">
                <thead>
                  <tr>
                    <th scope="col">Run</th>
                    <th scope="col">Started</th>
                    <th scope="col">Duration</th>
                    <th scope="col">Epochs</th>
                    <th scope="col">Final Loss</th>
                    <th scope="col">Status</th>
                    <th scope="col"><span class="sr-only">Details</span></th>
                  </tr>
                </thead>
                <tbody>
                  {#each history as run (run.id ?? run.run_id)}
                    <tr
                      class="history-row"
                      class:expanded={expandedRunId === (run.id ?? run.run_id)}
                      aria-expanded={expandedRunId === (run.id ?? run.run_id)}
                    >
                      <td class="run-id">{run.id ?? run.run_id ?? '—'}</td>
                      <td>{fmt.datetime(run.started_at)}</td>
                      <td>{run.duration_ms != null ? fmt.duration(run.duration_ms) : '—'}</td>
                      <td>{run.epochs_completed ?? run.epochs ?? '—'}</td>
                      <td class="font-mono">{run.final_loss != null ? fmt.decimal(run.final_loss, 4) : '—'}</td>
                      <td>
                        <span class="status-badge status-{runStatusColor(run.state ?? run.status)}">
                          {run.state ?? run.status ?? 'unknown'}
                        </span>
                      </td>
                      <td>
                        <button
                          class="btn-icon"
                          onclick={() => toggleRunDetails(run.id ?? run.run_id)}
                          aria-label="Toggle run details for {run.id ?? run.run_id}"
                        >
                          <i class="fas fa-chevron-{expandedRunId === (run.id ?? run.run_id) ? 'up' : 'down'}"></i>
                        </button>
                      </td>
                    </tr>
                    {#if expandedRunId === (run.id ?? run.run_id)}
                      <tr class="run-detail-row" aria-label="Run details">
                        <td colspan="7">
                          <dl class="run-detail-grid">
                            <dt>Model</dt><dd>{run.model ?? '—'}</dd>
                            <dt>LR</dt><dd>{run.learning_rate ?? '—'}</dd>
                            <dt>Batch</dt><dd>{run.batch_size ?? '—'}</dd>
                            <dt>Samples</dt><dd>{run.num_samples != null ? fmt.integer(run.num_samples) : '—'}</dd>
                            <dt>Steps</dt><dd>{run.total_steps != null ? fmt.integer(run.total_steps) : '—'}</dd>
                            {#if run.error}
                              <dt>Error</dt><dd class="text-danger">{run.error}</dd>
                            {/if}
                          </dl>
                        </td>
                      </tr>
                    {/if}
                  {/each}
                </tbody>
              </table>
            </div>
          {/if}
        </section>
      </div>

      <!-- Right column: idle training / kaizen -->
      <aside class="training-sidebar" aria-label="Idle training and kaizen stats">
        <section class="card idle-card">
          <div class="card-header">
            <h3><i class="fas fa-moon"></i> Idle Training</h3>
            {#if idleStats != null}
              <span class="status-badge status-{idleStats != null ? 'success' : 'muted'}">
                {idleStats != null ? 'On' : 'Off'}
              </span>
            {/if}
          </div>
          {#if idleStats}
            <dl class="stats-list">
              <dt>Sessions</dt>
              <dd>{fmt.integer(idleStats.idle_sessions?.length ?? 0)}</dd>
              <dt>Samples trained</dt>
              <dd>{fmt.integer(idleStats.samples_trained ?? 0)}</dd>
              <dt>Avg improvement</dt>
              <dd>{fmt.percent(idleStats.avg_improvement ?? 0)}</dd>
              <dt>Last idle run</dt>
              <dd>{fmt.relativeTime(idleStats.last_run)}</dd>
              <dt>Next scheduled</dt>
              <dd>{idleStats.next_scheduled ? fmt.datetime(idleStats.next_scheduled) : '—'}</dd>
            </dl>
          {:else}
            <p class="text-muted">Idle training stats unavailable.</p>
          {/if}
        </section>

        <section class="card kaizen-card">
          <div class="card-header">
            <h3><i class="fas fa-chart-line"></i> Kaizen Metrics</h3>
          </div>
          {#if idleStats?.kaizen_score ?? null}
            <dl class="stats-list">
              <dt>Kaizen score</dt>
              <dd>{fmt.decimal(idleStats.kaizen_score ?? 0, 3)}</dd>
            </dl>
          {:else}
            <p class="text-muted">No kaizen data yet.</p>
          {/if}
        </section>
      </aside>
    </div>
  {/if}
</div>

<style>
  .training-view {
    padding: 24px;
    max-width: 1400px;
  }

  .view-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 24px;
    gap: 12px;
  }

  .view-header h2 {
    font-size: 1.25rem;
    font-weight: 600;
    color: var(--text-primary);
    margin: 0;
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .view-header h2 i { color: var(--warning); }

  .loading-state {
    display: flex;
    align-items: center;
    gap: 10px;
    color: var(--text-muted);
    padding: 48px 24px;
    font-size: 0.9375rem;
  }

  .training-layout {
    display: grid;
    grid-template-columns: 1fr 280px;
    gap: 20px;
    align-items: start;
  }

  .training-main {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  .training-sidebar {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  /* Cards */
  .card {
    background: var(--surface-elevated);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-lg);
    padding: 16px;
  }

  .card-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 14px;
  }

  .card-header h3 {
    font-size: 0.9375rem;
    font-weight: 600;
    color: var(--text-primary);
    margin: 0;
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .card-header h3 i { color: var(--text-muted); }

  /* Status badge */
  .status-badge {
    display: inline-flex;
    align-items: center;
    padding: 2px 10px;
    border-radius: var(--radius-full);
    font-size: 0.75rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }

  .status-success { background: var(--success-muted); color: var(--success); }
  .status-warning { background: var(--warning-muted); color: var(--warning); }
  .status-danger  { background: var(--danger-muted);  color: var(--danger);  }
  .status-primary { background: var(--primary-muted); color: var(--primary); }
  .status-muted   { background: var(--surface-hover); color: var(--text-muted); }

  /* Progress */
  .progress-block { margin-bottom: 16px; }

  .progress-meta {
    display: flex;
    justify-content: space-between;
    font-size: 0.8125rem;
    color: var(--text-secondary);
    margin-bottom: 6px;
  }

  .progress-bar {
    height: 6px;
    background: var(--surface-hover);
    border-radius: var(--radius-full);
    overflow: hidden;
  }

  .progress-fill {
    height: 100%;
    background: var(--primary);
    border-radius: var(--radius-full);
    transition: width var(--transition-slow);
  }

  .progress-detail {
    display: flex;
    gap: 16px;
    font-size: 0.75rem;
    color: var(--text-muted);
    margin-top: 6px;
    flex-wrap: wrap;
  }

  .status-last-run, .status-empty {
    font-size: 0.875rem;
    color: var(--text-muted);
    margin-bottom: 14px;
  }

  /* Controls */
  .controls-row {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }

  /* Buttons */
  .btn {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 8px 14px;
    border-radius: var(--radius-md);
    font-size: 0.875rem;
    font-weight: 500;
    font-family: inherit;
    cursor: pointer;
    border: none;
    transition: background var(--transition-base), opacity var(--transition-base);
  }

  .btn:disabled { opacity: 0.5; cursor: not-allowed; }
  .btn-sm { padding: 6px 10px; font-size: 0.8125rem; }

  .btn-primary { background: var(--primary); color: var(--text-on-primary); }
  .btn-primary:hover:not(:disabled) { background: var(--primary-hover); }

  .btn-secondary { background: var(--surface-hover); color: var(--text-primary); border: 1px solid var(--border-default); }
  .btn-secondary:hover:not(:disabled) { background: var(--surface-pressed); }

  .btn-warning { background: var(--warning-muted); color: var(--warning); border: 1px solid rgba(245,165,36,0.3); }
  .btn-warning:hover:not(:disabled) { background: rgba(245,165,36,0.2); }

  .btn-danger { background: var(--danger-muted); color: var(--danger); border: 1px solid rgba(240,98,98,0.3); }
  .btn-danger:hover:not(:disabled) { background: rgba(240,98,98,0.2); }

  .btn-icon {
    background: none;
    border: none;
    cursor: pointer;
    color: var(--text-muted);
    padding: 4px 6px;
    border-radius: var(--radius-sm);
    font-size: 0.875rem;
  }

  .btn-icon:hover { background: var(--surface-hover); color: var(--text-primary); }

  /* Forms */
  .form-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
    gap: 12px;
    margin-bottom: 14px;
  }

  .form-group { display: flex; flex-direction: column; gap: 4px; }

  .form-label {
    font-size: 0.75rem;
    font-weight: 500;
    color: var(--text-secondary);
  }

  .input {
    background: var(--surface-bg);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    color: var(--text-primary);
    font-family: inherit;
    font-size: 0.875rem;
    padding: 6px 10px;
    width: 100%;
  }

  .input:focus {
    outline: 2px solid transparent;
    outline-offset: 2px;
    border-color: var(--primary);
    box-shadow: 0 0 0 2px var(--primary-muted);
  }

  .input:focus-visible {
    outline-color: var(--primary);
  }

  /* Config summary */
  .config-summary {
    display: flex;
    flex-wrap: wrap;
    gap: 8px 20px;
    margin: 0;
    font-size: 0.8125rem;
  }

  .config-summary dt { color: var(--text-muted); }
  .config-summary dd { color: var(--text-primary); margin: 0; font-family: var(--font-mono); }

  /* History table */
  .history-table-wrap {
    overflow-x: auto;
    border-radius: var(--radius-md);
    border: 1px solid var(--border-default);
  }

  .history-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.8125rem;
  }

  .history-table th {
    background: var(--surface-hover);
    color: var(--text-muted);
    font-weight: 600;
    text-align: left;
    padding: 8px 12px;
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }

  .history-table td {
    padding: 9px 12px;
    color: var(--text-primary);
    border-top: 1px solid var(--border-subtle);
  }

  .history-row:hover td { background: var(--surface-hover); }
  .history-row.expanded td { background: var(--primary-lighter); }

  .run-id { font-family: var(--font-mono); font-size: 0.75rem; color: var(--text-muted); }
  .font-mono { font-family: var(--font-mono); }

  .run-detail-row td { background: var(--surface-bg) !important; padding: 10px 16px; }

  .run-detail-grid {
    display: flex;
    flex-wrap: wrap;
    gap: 6px 20px;
    margin: 0;
    font-size: 0.8125rem;
  }

  .run-detail-grid dt { color: var(--text-muted); }
  .run-detail-grid dd { color: var(--text-primary); margin: 0; font-family: var(--font-mono); }

  /* Sidebar stats */
  .stats-list {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 6px 12px;
    margin: 0;
    font-size: 0.8125rem;
  }

  .stats-list dt { color: var(--text-muted); align-self: center; }
  .stats-list dd { color: var(--text-primary); margin: 0; font-weight: 500; text-align: right; }

  .badge {
    background: var(--surface-hover);
    color: var(--text-muted);
    border-radius: var(--radius-full);
    padding: 1px 8px;
    font-size: 0.75rem;
    font-weight: 600;
  }

  .empty-state {
    text-align: center;
    padding: 32px;
    color: var(--text-muted);
    font-size: 0.9375rem;
  }

  .empty-state i {
    font-size: 1.75rem;
    margin-bottom: 8px;
    display: block;
    opacity: 0.5;
  }

  .text-muted { color: var(--text-muted); font-size: 0.875rem; }
  .text-success { color: var(--success); }
  .text-danger { color: var(--danger); }

  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0,0,0,0);
    white-space: nowrap;
    border: 0;
  }

  @media (max-width: 1024px) {
    .training-layout { grid-template-columns: 1fr; }
  }
</style>
