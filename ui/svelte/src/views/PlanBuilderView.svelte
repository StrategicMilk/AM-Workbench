<script>
  /**
   * Plan builder / task decomposition view — goal input, template selection,
   * decomposition knobs, visual plan tree, DoD/DoR templates, and history.
   */
  import * as api from '$lib/api.js';
  import * as fmt from '$lib/utils/format.js';
  import { collectAll } from '$lib/evidence/collectErrors.js';
  import { showToast } from '$lib/stores/toast.svelte.js';
  import HelpPopover from '$lib/components/help/HelpPopover.svelte';
  import HelpTooltip from '$lib/components/help/HelpTooltip.svelte';

  // -- State -------------------------------------------------------------------

  let templates = $state([]);
  let dodDor = $state(null);
  let knobs = $state({});
  let history = $state([]);
  let loading = $state(true);
  let decomposing = $state(false);

  let goalInput = $state('');
  let selectedTemplate = $state('');
  let useAgent = $state(false);

  let decompositionResult = $state(null);
  let expandedHistoryId = $state(null);
  let expandedSubtask = $state(null);
  let activeTab = $state('decompose');

  // Knob overrides that the user can tweak
  let knobOverrides = $state({});

  // Active plan execution state
  let activePlanId = $state(null);
  let planStatus = $state(null);
  let planStatusError = $state('');
  let planActionPending = $state(false);

  // -- Derived -----------------------------------------------------------------

  let activeKnobs = $derived({ ...knobs, ...knobOverrides });

  let planIsExecuting = $derived(
    planStatus === 'executing' || planStatus === 'running' || planStatus === 'in_progress'
  );
  let planIsPaused = $derived(planStatus === 'paused');
  let planIsTerminal = $derived(
    planStatus === 'completed' || planStatus === 'complete' ||
    planStatus === 'failed' || planStatus === 'cancelled'
  );

  const planBuilderTabIds = ['decompose', 'result', 'dod', 'history'];

  let subtasks = $derived(
    decompositionResult?.subtasks ??
    decompositionResult?.tasks ??
    []
  );

  // -- Data loading ------------------------------------------------------------

  async function loadAll() {
    loading = true;
    try {
      const { values, errors } = await collectAll([
        { key: 'templates', promise: api.getDecompositionTemplates() },
        { key: 'dodDor', promise: api.getDodDor() },
        { key: 'knobs', promise: api.getDecompositionKnobs() },
        { key: 'history', promise: api.getDecompositionHistory() },
      ]);

      if (errors.length > 0) {
        showToast(`Plan builder load partial: ${errors.map((entry) => entry.key).join(', ')}`, 'warning');
      }

      const tmplData = values.templates;
      const dodData = values.dodDor;
      const knobData = values.knobs;
      const histData = values.history;

      templates = tmplData?.templates ?? (Array.isArray(tmplData) ? tmplData : []);
      dodDor = dodData ?? null;
      knobs = knobData?.knobs ?? (typeof knobData === 'object' && knobData !== null ? knobData : {});
      history = histData?.history ?? (Array.isArray(histData) ? histData : []);
    } catch (err) {
      showToast(`Failed to load plan builder: ${err.message}`, 'error');
    } finally {
      loading = false;
    }
  }

  async function refreshPlanBuilder() {
    await loadAll();
    await refreshPlanStatus();
  }

  $effect(() => { loadAll(); });

  // -- Decompose ---------------------------------------------------------------

  async function handleDecompose(e) {
    e.preventDefault();
    if (!goalInput.trim()) {
      showToast('Please enter a goal to decompose', 'warning');
      return;
    }

    decomposing = true;
    decompositionResult = null;

    const payload = {
      goal: goalInput,
      template: selectedTemplate || undefined,
      knobs: Object.keys(knobOverrides).length > 0 ? knobOverrides : undefined,
    };

    try {
      const result = useAgent
        ? await api.decomposeWithAgent(payload)
        : await api.decompose(payload);
      decompositionResult = result;
      activePlanId = result?.plan_id ?? result?.id ?? null;
      planStatus = result?.status ?? (activePlanId ? 'executing' : null);
      planStatusError = '';
      showToast(`Decomposed into ${subtasks.length} subtasks`, 'success');
      activeTab = 'result';
      await loadHistory();
    } catch (err) {
      showToast(`Decomposition failed: ${err.message}`, 'error');
    } finally {
      decomposing = false;
    }
  }

  async function loadHistory() {
    try {
      const h = await api.getDecompositionHistory();
      history = h?.history ?? (Array.isArray(h) ? h : []);
    } catch {
      // Non-critical
    }
  }

  // -- Plan execution controls -------------------------------------------------

  async function refreshPlanStatus() {
    if (!activePlanId) return;
    try {
      const data = await api.getPlanStatus(activePlanId);
      // Replace optimistic local planStatus with the backend-confirmed value.
      if (data?.status !== undefined) {
        planStatus = data.status;
        planStatusError = '';
      }
    } catch (err) {
      planStatus = null;
      planStatusError = err?.message ?? 'Plan status unavailable';
    }
  }

  function tabIsVisible() {
    return typeof document === 'undefined' || document.visibilityState === 'visible';
  }

  function refreshPlanStatusWhenActive() {
    if (tabIsVisible()) {
      void refreshPlanStatus();
    }
  }

  // Refresh live plan status when the view activates and after app-level
  // refresh signals. The project stream is single-subscriber today, so this
  // view avoids opening a competing SSE subscription.
  $effect(() => {
    if (!activePlanId || planIsTerminal) return;

    // Fetch immediately on activation so the badge is correct right away.
    refreshPlanStatusWhenActive();

    const handleVisibilityChange = () => refreshPlanStatusWhenActive();
    document.addEventListener('visibilitychange', handleVisibilityChange);
    window.addEventListener('focus', refreshPlanStatusWhenActive);
    window.addEventListener('pageshow', refreshPlanStatusWhenActive);
    window.addEventListener('online', refreshPlanStatusWhenActive);
    window.addEventListener('vetinari:plan-status-refresh', refreshPlanStatusWhenActive);
    return () => {
      document.removeEventListener('visibilitychange', handleVisibilityChange);
      window.removeEventListener('focus', refreshPlanStatusWhenActive);
      window.removeEventListener('pageshow', refreshPlanStatusWhenActive);
      window.removeEventListener('online', refreshPlanStatusWhenActive);
      window.removeEventListener('vetinari:plan-status-refresh', refreshPlanStatusWhenActive);
    };
  });

  async function handlePausePlan() {
    if (!activePlanId) return;
    planActionPending = true;
    try {
      await api.pausePlan(activePlanId);
      planStatus = 'paused';
      planStatusError = '';
      showToast('Plan paused', 'info');
    } catch (err) {
      showToast(`Pause failed: ${err.message}`, 'error');
    } finally {
      planActionPending = false;
    }
  }

  async function handleResumePlan() {
    if (!activePlanId) return;
    planActionPending = true;
    try {
      await api.resumePlan(activePlanId);
      planStatus = 'executing';
      planStatusError = '';
      showToast('Plan resumed', 'success');
    } catch (err) {
      showToast(`Resume failed: ${err.message}`, 'error');
    } finally {
      planActionPending = false;
    }
  }

  async function handleCancelPlan() {
    if (!activePlanId) return;
    if (!confirm('Cancel this plan? This cannot be undone.')) return;
    planActionPending = true;
    try {
      await api.cancelPlan(activePlanId);
      planStatus = 'cancelled';
      planStatusError = '';
      showToast('Plan cancelled', 'info');
    } catch (err) {
      showToast(`Cancel failed: ${err.message}`, 'error');
    } finally {
      planActionPending = false;
    }
  }

  function selectHistoryResult(item) {
    decompositionResult = item.result ?? item;
    goalInput = item.goal ?? item.input ?? '';
    activeTab = 'result';
  }

  function subtaskDepth(subtask) {
    return subtask.depth ?? subtask.level ?? 0;
  }

  function subtaskStatusColor(status) {
    const map = { completed: 'success', in_progress: 'primary', failed: 'danger', pending: 'warning' };
    return map[status ?? 'pending'] ?? 'muted';
  }

  function movePlanBuilderTabFocus(event, tabId) {
    const currentIndex = planBuilderTabIds.indexOf(tabId);
    if (currentIndex === -1) return;

    let nextIndex = currentIndex;
    if (event.key === 'ArrowRight' || event.key === 'ArrowDown') {
      nextIndex = (currentIndex + 1) % planBuilderTabIds.length;
    } else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') {
      nextIndex = (currentIndex - 1 + planBuilderTabIds.length) % planBuilderTabIds.length;
    } else if (event.key === 'Home') {
      nextIndex = 0;
    } else if (event.key === 'End') {
      nextIndex = planBuilderTabIds.length - 1;
    } else {
      return;
    }

    event.preventDefault();
    activeTab = planBuilderTabIds[nextIndex];
    document.getElementById(`tab-${activeTab}`)?.focus();
  }
</script>

<div class="plan-builder-view">
  <div class="view-header">
    <h2>
      <i class="fas fa-project-diagram" aria-hidden="true"></i>
      Plan Builder
    </h2>
    <HelpPopover
      title="Plan Builder"
      body="Visual editor for the Foreman's execution plan. The plan is a directed acyclic graph (DAG) of tasks: each node is a task with an assigned agent type and a description; each edge is a dependency. You can add tasks, edit descriptions, reassign agent types, and draw or delete dependency edges before submitting. Once submitted, the Workbench locks the plan and begins executing tasks in dependency order. Circular dependencies are rejected at submission time."
      severity="info"
    />
    <button
      class="btn btn-secondary btn-sm"
      onclick={refreshPlanBuilder}
      disabled={loading}
      aria-label="Refresh plan builder"
    >
      <i class="fas fa-sync-alt" class:fa-spin={loading} aria-hidden="true"></i>
    </button>
  </div>

  {#if loading}
    <div class="loading-state" role="status" aria-live="polite">
      <i class="fas fa-spinner fa-spin" aria-hidden="true"></i>
      Loading plan builder...
    </div>
  {:else}
    <!-- Tabs -->
    <div class="tab-bar" role="tablist" aria-label="Plan builder sections">
      {#each [
        { id: 'decompose', icon: 'fas fa-cut', label: 'Decompose' },
        { id: 'result', icon: 'fas fa-sitemap', label: 'Plan Tree' },
        { id: 'dod', icon: 'fas fa-check-double', label: 'DoD/DoR' },
        { id: 'history', icon: 'fas fa-history', label: 'History' },
      ] as tab (tab.id)}
        <button
          class="tab"
          class:active={activeTab === tab.id}
          onclick={() => { activeTab = tab.id; }}
          onkeydown={(event) => movePlanBuilderTabFocus(event, tab.id)}
          role="tab"
          aria-selected={activeTab === tab.id}
          aria-controls="panel-{tab.id}"
          id="tab-{tab.id}"
          tabindex={activeTab === tab.id ? 0 : -1}
        >
          <i class="{tab.icon}" aria-hidden="true"></i>
          {tab.label}
          {#if tab.id === 'result' && subtasks.length > 0}
            <span class="badge">{subtasks.length}</span>
          {/if}
          {#if tab.id === 'history' && history.length > 0}
            <span class="badge">{history.length}</span>
          {/if}
        </button>
      {/each}
    </div>

    <!-- Decompose panel -->
    {#if activeTab === 'decompose'}
      <div id="panel-decompose" role="tabpanel" aria-labelledby="tab-decompose" class="panel-content">
        <div class="decompose-layout">
          <section class="card decompose-form-card">
            <h3 class="section-title"><i class="fas fa-bullseye" aria-hidden="true"></i> Goal</h3>
            <form onsubmit={handleDecompose} class="decompose-form" aria-label="Task decomposition form">
              <label class="form-group">
                <span class="form-label">Describe the task or goal to decompose</span>
                <textarea
                  class="input textarea"
                  bind:value={goalInput}
                  rows="5"
                  placeholder="e.g. Build a REST API for user authentication with JWT tokens..."
                  required
                  aria-required="true"
                  aria-label="Goal description"
                  disabled={decomposing}
                ></textarea>
              </label>

              {#if templates.length > 0}
                <label class="form-group">
                  <span class="form-label">Template (optional)</span>
                  <select
                    class="input"
                    bind:value={selectedTemplate}
                    aria-label="Decomposition template"
                    disabled={decomposing}
                  >
                    <option value="">No template</option>
                    {#each templates as tmpl ((tmpl.id ?? tmpl.name))}
                      <option value={tmpl.id ?? tmpl.name}>{tmpl.label ?? tmpl.name}</option>
                    {/each}
                  </select>
                </label>
              {/if}

              <div class="mode-row">
                <button
                  type="button"
                  class="toggle-mode"
                  class:active={useAgent}
                  onclick={() => { useAgent = !useAgent; }}
                  aria-pressed={useAgent}
                  aria-label="Use AI agent for decomposition"
                >
                  <i class="fas fa-robot" aria-hidden="true"></i>
                  {useAgent ? 'Agent mode' : 'Template mode'}
                </button>
                <span class="mode-desc">
                  {useAgent ? 'Agent autonomously decomposes using LLM reasoning.' : 'Rule-based decomposition with templates.'}
                </span>
              </div>

              <button
                type="submit"
                class="btn btn-primary btn-lg"
                disabled={decomposing || !goalInput.trim()}
                aria-label="Decompose goal into subtasks"
              >
                {#if decomposing}
                  <i class="fas fa-spinner fa-spin" aria-hidden="true"></i>
                  Decomposing...
                {:else}
                  <i class="fas fa-cut" aria-hidden="true"></i>
                  Decompose
                {/if}
              </button>
            </form>
          </section>

          <!-- Knobs panel -->
          {#if Object.keys(knobs).length > 0}
            <aside class="card knobs-card" aria-label="Decomposition knobs">
              <h3 class="section-title"><i class="fas fa-sliders-h" aria-hidden="true"></i> Knobs</h3>
              <div class="knobs-list">
                {#each Object.entries(knobs) as [key, defaultVal] (key)}
                  <label class="form-group">
                    <span class="form-label">{key}</span>
                    <input
                      type={typeof defaultVal === 'number' ? 'number' : 'text'}
                      class="input"
                      value={knobOverrides[key] ?? defaultVal}
                      oninput={(e) => { knobOverrides = { ...knobOverrides, [key]: e.target.value }; }}
                      aria-label="Knob: {key}"
                    />
                  </label>
                {/each}
              </div>
              {#if Object.keys(knobOverrides).length > 0}
                <button
                  class="btn btn-secondary btn-sm"
                  onclick={() => { knobOverrides = {}; }}
                  aria-label="Reset knobs to defaults"
                >
                  <i class="fas fa-undo" aria-hidden="true"></i> Reset
                </button>
              {/if}
            </aside>
          {/if}
        </div>
      </div>
    {/if}

    <!-- Plan tree panel -->
    {#if activeTab === 'result'}
      <div id="panel-result" role="tabpanel" aria-labelledby="tab-result" class="panel-content">
        {#if !decompositionResult}
          <div class="empty-state">
            <i class="fas fa-sitemap" aria-hidden="true"></i>
            <p>No decomposition result yet. Enter a goal and click Decompose.</p>
            <button class="btn btn-primary" onclick={() => { activeTab = 'decompose'; }}>
              Go to Decompose
            </button>
          </div>
        {:else}
          <section class="card result-card" aria-label="Decomposition result">
            <div class="result-header">
              <h3 class="section-title"><i class="fas fa-sitemap" aria-hidden="true"></i> Plan Tree</h3>
              <div class="result-meta">
                {#if decompositionResult.goal ?? goalInput}
                  <span class="result-goal">{decompositionResult.goal ?? goalInput}</span>
                {/if}
                <span class="badge">{subtasks.length} tasks</span>
                {#if decompositionResult.estimated_tokens != null}
                  <span class="result-est">~{fmt.integer(decompositionResult.estimated_tokens)} tokens</span>
                {/if}
                {#if planStatus && !planIsTerminal}
                  <span class="plan-status-badge plan-status-{planIsPaused ? 'paused' : 'running'}">
                    <i class="fas {planIsPaused ? 'fa-pause' : 'fa-play'}" aria-hidden="true"></i>
                    {planIsPaused ? 'Paused' : 'Executing'}
                  </span>
                {/if}
                {#if planStatus === 'cancelled'}
                  <span class="plan-status-badge plan-status-cancelled">
                    <i class="fas fa-ban" aria-hidden="true"></i>
                    Cancelled
                  </span>
                {/if}
                {#if planStatus === 'completed' || planStatus === 'complete'}
                  <span class="plan-status-badge plan-status-done">
                    <i class="fas fa-check" aria-hidden="true"></i>
                    Completed
                  </span>
                {/if}
                {#if planStatusError}
                  <span class="plan-status-badge plan-status-error" title={planStatusError}>
                    <i class="fas fa-exclamation-triangle" aria-hidden="true"></i>
                    Status unavailable
                  </span>
                {/if}
              </div>
            </div>

            {#if activePlanId && (planIsExecuting || planIsPaused)}
              <div class="plan-controls" aria-label="Plan execution controls">
                {#if planIsExecuting}
                  <button
                    class="btn btn-secondary btn-sm"
                    onclick={handlePausePlan}
                    disabled={planActionPending}
                    aria-label="Pause plan execution"
                    title="Pause"
                  >
                    <i class="fas fa-pause" aria-hidden="true"></i>
                    Pause
                  </button>
                {/if}
                {#if planIsPaused}
                  <button
                    class="btn btn-primary btn-sm"
                    onclick={handleResumePlan}
                    disabled={planActionPending}
                    aria-label="Resume plan execution"
                    title="Resume"
                  >
                    <i class="fas fa-play" aria-hidden="true"></i>
                    Resume
                  </button>
                {/if}
                <button
                  class="btn btn-danger btn-sm"
                  onclick={handleCancelPlan}
                  disabled={planActionPending}
                  aria-label="Cancel plan execution"
                  title="Cancel"
                >
                  <i class="fas fa-stop" aria-hidden="true"></i>
                  Cancel
                </button>
              </div>
            {/if}

            {#if subtasks.length === 0}
              <div class="empty-state">
                <i class="fas fa-inbox" aria-hidden="true"></i>
                <p>No subtasks generated.</p>
              </div>
            {:else}
              <ul class="plan-tree" aria-label="Decomposed plan subtasks">
                {#each subtasks as subtask, i ((subtask.id ?? subtask.subtask_id ?? i))}
                  {@const sid = subtask.id ?? subtask.subtask_id ?? String(i)}
                  {@const depth = subtaskDepth(subtask)}
                  <li
                    class="plan-node"
                    style="--depth: {depth}"
                    aria-label="Task {i + 1}: {subtask.name ?? subtask.description}"
                  >
                    <button
                      class="plan-node-btn"
                      onclick={() => { expandedSubtask = expandedSubtask === sid ? null : sid; }}
                      aria-expanded={expandedSubtask === sid}
                      aria-label="Toggle subtask: {subtask.name ?? subtask.description ?? sid}"
                    >
                      <span class="node-num">{i + 1}</span>
                      <span class="node-title">{subtask.name ?? subtask.description ?? 'Unnamed task'}</span>
                      {#if subtask.agent_type}
                        <span class="node-agent">{subtask.agent_type}</span>
                      {/if}
                      {#if subtask.estimated_tokens}
                        <span class="node-tokens">{fmt.integer(subtask.estimated_tokens)}t</span>
                      {/if}
                    </button>
                    {#if expandedSubtask === sid}
                      <div class="node-detail" role="region" aria-label="Subtask details">
                        {#if subtask.description && subtask.description !== subtask.name}
                          <p class="node-desc">{subtask.description}</p>
                        {/if}
                        <dl class="node-meta-list">
                          {#if subtask.inputs?.length > 0}
                            <dt>Inputs</dt><dd>{subtask.inputs.join(', ')}</dd>
                          {/if}
                          {#if subtask.outputs?.length > 0}
                            <dt>Outputs</dt><dd>{subtask.outputs.join(', ')}</dd>
                          {/if}
                          {#if subtask.dependencies?.length > 0}
                            <dt>Depends on</dt><dd>{subtask.dependencies.join(', ')}</dd>
                          {/if}
                          {#if subtask.acceptance_criteria}
                            <dt>Acceptance</dt><dd>{subtask.acceptance_criteria}</dd>
                          {/if}
                        </dl>
                      </div>
                    {/if}
                  </li>
                {/each}
              </ul>
            {/if}
          </section>
        {/if}
      </div>
    {/if}

    <!-- DoD/DoR panel -->
    {#if activeTab === 'dod'}
      <div id="panel-dod" role="tabpanel" aria-labelledby="tab-dod" class="panel-content">
        {#if !dodDor}
          <div class="empty-state">
            <i class="fas fa-check-double" aria-hidden="true"></i>
            <p>No DoD/DoR templates available.</p>
          </div>
        {:else}
          <div class="dod-layout">
            {#if dodDor.definition_of_done}
              <section class="card dod-card" aria-label="Definition of Done">
                <h3 class="section-title">
                  <i class="fas fa-check-circle text-success" aria-hidden="true"></i>
                  Definition of Done
                </h3>
                <ul class="checklist" aria-label="Definition of Done checklist">
                  {#each (Array.isArray(dodDor.definition_of_done) ? dodDor.definition_of_done : [dodDor.definition_of_done]) as item, i (i)}
                    <li class="checklist-item">
                      <i class="fas fa-circle-check check-icon" aria-hidden="true"></i>
                      <span>{item}</span>
                    </li>
                  {/each}
                </ul>
              </section>
            {/if}
            {#if dodDor.definition_of_ready}
              <section class="card dor-card" aria-label="Definition of Ready">
                <h3 class="section-title">
                  <i class="fas fa-play-circle text-primary" aria-hidden="true"></i>
                  Definition of Ready
                </h3>
                <ul class="checklist" aria-label="Definition of Ready checklist">
                  {#each (Array.isArray(dodDor.definition_of_ready) ? dodDor.definition_of_ready : [dodDor.definition_of_ready]) as item, i (i)}
                    <li class="checklist-item">
                      <i class="fas fa-circle-check check-icon" aria-hidden="true"></i>
                      <span>{item}</span>
                    </li>
                  {/each}
                </ul>
              </section>
            {/if}
          </div>
        {/if}
      </div>
    {/if}

    <!-- History panel -->
    {#if activeTab === 'history'}
      <div id="panel-history" role="tabpanel" aria-labelledby="tab-history" class="panel-content">
        {#if history.length === 0}
          <div class="empty-state">
            <i class="fas fa-history" aria-hidden="true"></i>
            <p>No decomposition history yet.</p>
          </div>
        {:else}
          <ul class="history-list" aria-label="Decomposition history">
            {#each history as item, i ((item.id ?? i))}
              {@const iid = item.id ?? String(i)}
              <li class="history-item">
                <button
                  class="history-btn"
                  onclick={() => { expandedHistoryId = expandedHistoryId === iid ? null : iid; }}
                  aria-expanded={expandedHistoryId === iid}
                  aria-label="Toggle history item: {item.goal ?? item.input}"
                >
                  <div class="history-row">
                    <span class="history-goal">{item.goal ?? item.input ?? 'Unnamed'}</span>
                    <span class="history-meta">
                      {#if item.subtask_count ?? item.result?.subtasks?.length}
                        <span class="badge">{item.subtask_count ?? item.result?.subtasks?.length ?? 0} tasks</span>
                      {/if}
                      <span class="history-date">{fmt.relativeTime(item.created_at)}</span>
                    </span>
                  </div>
                </button>
                {#if expandedHistoryId === iid}
                  <div class="history-detail" role="region" aria-label="History item details">
                    <div class="history-actions">
                      <button
                        class="btn btn-primary btn-sm"
                        onclick={() => selectHistoryResult(item)}
                        aria-label="Load this decomposition result"
                      >
                        <i class="fas fa-upload" aria-hidden="true"></i> Load Result
                      </button>
                    </div>
                    {#if item.result?.subtasks ?? item.subtasks}
                      {@const sTasks = item.result?.subtasks ?? item.subtasks ?? []}
                      <ul class="history-subtasks" aria-label="History subtasks">
                        {#each sTasks as st, si (si)}
                          <li class="history-subtask-item">
                            <span class="subtask-num">{si + 1}</span>
                            <span>{st.name ?? st.description ?? 'unnamed'}</span>
                          </li>
                        {/each}
                      </ul>
                    {/if}
                  </div>
                {/if}
              </li>
            {/each}
          </ul>
        {/if}
      </div>
    {/if}
  {/if}
</div>

<style>
  .plan-builder-view {
    padding: 24px;
    max-width: 1200px;
  }

  .view-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 20px;
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

  .view-header h2 i { color: var(--secondary); }

  /* Tab bar */
  .tab-bar {
    display: flex;
    gap: 4px;
    border-bottom: 1px solid var(--border-default);
    margin-bottom: 24px;
    flex-wrap: wrap;
  }

  .tab {
    padding: 10px 16px;
    background: none;
    border: none;
    border-bottom: 2px solid transparent;
    color: var(--text-muted);
    font-size: 0.875rem;
    font-weight: 500;
    cursor: pointer;
    font-family: inherit;
    display: flex;
    align-items: center;
    gap: 6px;
    transition: color var(--transition-base), border-color var(--transition-base);
    margin-bottom: -1px;
  }

  .tab.active { color: var(--primary); border-bottom-color: var(--primary); }
  .tab:hover:not(.active) { color: var(--text-primary); }

  .panel-content { display: flex; flex-direction: column; gap: 16px; }

  /* Cards */
  .card {
    background: var(--surface-elevated);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-lg);
    padding: 16px;
  }

  .section-title {
    font-size: 0.9375rem;
    font-weight: 600;
    color: var(--text-primary);
    margin: 0 0 14px 0;
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .section-title i { color: var(--text-muted); }

  /* Decompose layout */
  .decompose-layout {
    display: grid;
    grid-template-columns: 1fr 280px;
    gap: 16px;
    align-items: start;
  }

  .decompose-form { display: flex; flex-direction: column; gap: 14px; }

  .form-group { display: flex; flex-direction: column; gap: 4px; }
  .form-label { font-size: 0.75rem; font-weight: 500; color: var(--text-secondary); }

  .input {
    background: var(--surface-bg);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    color: var(--text-primary);
    font-family: inherit;
    font-size: 0.875rem;
    padding: 6px 10px;
    width: 100%;
    box-sizing: border-box;
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

  .textarea { resize: vertical; }

  .mode-row {
    display: flex;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
  }

  .toggle-mode {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 7px 14px;
    background: var(--surface-hover);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-full);
    font-size: 0.8125rem;
    font-weight: 500;
    font-family: inherit;
    cursor: pointer;
    color: var(--text-secondary);
    transition: background var(--transition-base), color var(--transition-base);
    flex-shrink: 0;
  }

  .toggle-mode.active { background: var(--primary-muted); color: var(--primary); border-color: var(--primary); }

  .mode-desc { font-size: 0.8125rem; color: var(--text-muted); }

  /* Knobs */
  .knobs-list { display: flex; flex-direction: column; gap: 10px; margin-bottom: 12px; }

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
    transition: background var(--transition-base);
  }

  .btn:disabled { opacity: 0.5; cursor: not-allowed; }
  .btn-sm { padding: 6px 10px; font-size: 0.8125rem; }
  .btn-lg { padding: 10px 20px; font-size: 0.9375rem; }

  .btn-primary { background: var(--primary); color: var(--text-on-primary); }
  .btn-primary:hover:not(:disabled) { background: var(--primary-hover); }

  .btn-secondary { background: var(--surface-hover); color: var(--text-primary); border: 1px solid var(--border-default); }
  .btn-secondary:hover:not(:disabled) { background: var(--surface-pressed); }

  /* Plan tree */
  .result-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 12px;
    margin-bottom: 16px;
    flex-wrap: wrap;
  }

  .result-meta {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }

  .result-goal {
    font-size: 0.8125rem;
    color: var(--text-secondary);
    max-width: 400px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .result-est { font-size: 0.75rem; color: var(--text-muted); }

  .plan-tree {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .plan-node {
    margin-left: calc(var(--depth, 0) * 20px);
  }

  .plan-node-btn {
    width: 100%;
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 12px;
    background: var(--surface-bg);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    cursor: pointer;
    font-family: inherit;
    text-align: left;
    transition: background var(--transition-base);
  }

  .plan-node-btn:hover { background: var(--surface-hover); border-color: var(--border-default); }

  .node-num {
    width: 22px;
    height: 22px;
    background: var(--primary-muted);
    color: var(--primary);
    border-radius: var(--radius-full);
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 0.6875rem;
    font-weight: 700;
    flex-shrink: 0;
  }

  .node-title {
    flex: 1;
    font-size: 0.875rem;
    font-weight: 500;
    color: var(--text-primary);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .node-agent {
    font-size: 0.6875rem;
    background: var(--surface-hover);
    color: var(--text-muted);
    padding: 1px 6px;
    border-radius: var(--radius-full);
    flex-shrink: 0;
  }

  .node-tokens {
    font-size: 0.6875rem;
    color: var(--text-muted);
    font-family: var(--font-mono);
    flex-shrink: 0;
  }

  .node-detail {
    margin: 4px 0 0 30px;
    padding: 8px 12px;
    background: var(--surface-bg);
    border-left: 2px solid var(--primary-muted);
    border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
  }

  .node-desc { font-size: 0.8125rem; color: var(--text-muted); margin: 0 0 8px 0; }

  .node-meta-list {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: 4px 10px;
    margin: 0;
    font-size: 0.75rem;
  }

  .node-meta-list dt { color: var(--text-muted); }
  .node-meta-list dd { margin: 0; color: var(--text-secondary); }

  /* DoD/DoR */
  .dod-layout { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }

  .checklist { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }

  .checklist-item {
    display: flex;
    align-items: flex-start;
    gap: 8px;
    font-size: 0.875rem;
    color: var(--text-secondary);
    line-height: var(--leading-relaxed);
  }

  .check-icon { color: var(--success); flex-shrink: 0; margin-top: 2px; }

  /* History */
  .history-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }

  .history-item {
    background: var(--surface-elevated);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-md);
    overflow: hidden;
  }

  .history-btn {
    width: 100%;
    background: none;
    border: none;
    cursor: pointer;
    padding: 12px 14px;
    font-family: inherit;
    text-align: left;
    transition: background var(--transition-base);
  }

  .history-btn:hover { background: var(--surface-hover); }

  .history-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    flex-wrap: wrap;
  }

  .history-goal {
    font-size: 0.9375rem;
    font-weight: 500;
    color: var(--text-primary);
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .history-meta { display: flex; align-items: center; gap: 8px; flex-shrink: 0; }
  .history-date { font-size: 0.75rem; color: var(--text-muted); }

  .history-detail {
    padding: 10px 14px 12px;
    border-top: 1px solid var(--border-subtle);
  }

  .history-actions { margin-bottom: 10px; }

  .history-subtasks { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 4px; }

  .history-subtask-item {
    display: flex;
    gap: 8px;
    font-size: 0.8125rem;
    color: var(--text-secondary);
    padding: 4px 0;
    border-top: 1px solid var(--border-subtle);
  }

  .subtask-num {
    width: 18px;
    color: var(--text-muted);
    font-size: 0.75rem;
    flex-shrink: 0;
    text-align: right;
  }

  /* Badges */
  .badge {
    background: var(--surface-hover);
    color: var(--text-muted);
    border-radius: var(--radius-full);
    padding: 1px 7px;
    font-size: 0.6875rem;
    font-weight: 600;
  }

  /* Loading / empty */
  .loading-state {
    display: flex;
    align-items: center;
    gap: 10px;
    color: var(--text-muted);
    padding: 48px 24px;
  }

  .empty-state {
    text-align: center;
    padding: 64px 24px;
    color: var(--text-muted);
    font-size: 0.9375rem;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 12px;
  }

  .empty-state i { font-size: 2rem; opacity: 0.35; }
  .empty-state p { margin: 0; }

  .text-success { color: var(--success); }
  .text-primary { color: var(--primary); }

  /* Plan execution controls */
  .plan-controls {
    display: flex;
    gap: 8px;
    align-items: center;
    margin-bottom: 16px;
    padding-bottom: 14px;
    border-bottom: 1px solid var(--border-subtle);
    flex-wrap: wrap;
  }

  .plan-status-badge {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 2px 8px;
    border-radius: var(--radius-full);
    font-size: 0.6875rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    flex-shrink: 0;
  }

  .plan-status-running { background: var(--primary-muted); color: var(--primary); }
  .plan-status-paused  { background: var(--warning-muted); color: var(--warning); }
  .plan-status-cancelled { background: var(--surface-hover); color: var(--text-muted); }
  .plan-status-done    { background: var(--success-muted); color: var(--success); }
  .plan-status-error   { background: var(--danger-muted); color: var(--danger); }

  .btn-danger { background: var(--danger-muted); color: var(--danger); border: 1px solid rgba(240,98,98,0.3); }
  .btn-danger:hover:not(:disabled) { background: rgba(240,98,98,0.2); }

  @media (max-width: 900px) {
    .decompose-layout { grid-template-columns: 1fr; }
    .dod-layout { grid-template-columns: 1fr; }
  }
</style>
