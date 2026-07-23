<script>
  import { appState } from '$lib/stores/app.svelte.js';
  import Icon from '$lib/a11y/Icon.svelte';
  import * as fmt from '$lib/utils/format.js';

  let {
    tasks = [],
    loading = false,
    loadError = null,
    actionPending = false,
    onRerun,
    onCancel,
  } = $props();

  let searchQuery = $state('');
  let statusFilter = $state('all');
  let expandedId = $state(null);

  const STATUS_FILTERS = [
    { key: 'all', label: 'All' },
    { key: 'pending', label: 'Pending' },
    { key: 'in_progress', label: 'In Progress' },
    { key: 'completed', label: 'Completed' },
    { key: 'failed', label: 'Failed' },
  ];

  let filteredTasks = $derived(
    tasks.filter((task) => {
      if (statusFilter !== 'all') {
        const taskStatus = task.status ?? 'pending';
        if (statusFilter === 'in_progress') {
          if (taskStatus !== 'in_progress' && taskStatus !== 'running') return false;
        } else if (statusFilter === 'completed') {
          if (taskStatus !== 'completed' && taskStatus !== 'complete') return false;
        } else if (taskStatus !== statusFilter) {
          return false;
        }
      }
      if (!searchQuery.trim()) {
        return true;
      }
      const query = searchQuery.toLowerCase();
      return (
        (task.name ?? task.description ?? '').toLowerCase().includes(query) ||
        (task.id ?? '').toLowerCase().includes(query) ||
        (task.type ?? '').toLowerCase().includes(query) ||
        (task.agent ?? '').toLowerCase().includes(query)
      );
    })
  );

  let statusCounts = $derived({
    all: tasks.length,
    pending: tasks.filter((task) => !task.status || task.status === 'pending').length,
    in_progress: tasks.filter((task) => task.status === 'in_progress' || task.status === 'running').length,
    completed: tasks.filter((task) => task.status === 'completed' || task.status === 'complete').length,
    failed: tasks.filter((task) => task.status === 'failed').length,
  });

  function statusColor(status) {
    const map = {
      completed: 'success',
      complete: 'success',
      in_progress: 'primary',
      running: 'primary',
      failed: 'danger',
      pending: 'warning',
      cancelled: 'muted',
    };
    return map[status ?? 'pending'] ?? 'muted';
  }

  function statusLabel(status) {
    const map = {
      in_progress: 'In Progress',
      complete: 'Completed',
    };
    return map[status] ?? (status ? status.charAt(0).toUpperCase() + status.slice(1) : 'Pending');
  }

  function agentIcon(agentType) {
    const map = {
      foreman: 'hard-hat',
      worker: 'tools',
      inspector: 'search',
      planner: 'map',
    };
    return map[(agentType ?? '').toLowerCase()] ?? 'robot';
  }
</script>

<div class="filter-bar">
  <div class="search-wrap">
    <Icon name="search" class="search-icon" />
    <input
      type="search"
      class="input search-input"
      bind:value={searchQuery}
      placeholder="Search tasks..."
      aria-label="Search tasks"
    />
  </div>
  <div class="status-filters" role="group" aria-label="Filter tasks by status">
    {#each STATUS_FILTERS as filter (filter.key)}
      <button
        class="filter-btn"
        class:active={statusFilter === filter.key}
        onclick={() => { statusFilter = filter.key; }}
        aria-pressed={statusFilter === filter.key}
        aria-label="Show {filter.label} tasks ({statusCounts[filter.key] ?? 0})"
      >
        {filter.label}
        <span class="filter-count">{statusCounts[filter.key] ?? 0}</span>
      </button>
    {/each}
  </div>
</div>

{#if loading}
  <div class="loading-state" role="status" aria-live="polite">
    <Icon name="spinner" class="fa-spin" />
    Loading tasks...
  </div>
{:else if loadError}
  <div class="error-state" role="alert">
    <Icon name="exclamation-triangle" />
    <p>{loadError}</p>
  </div>
{:else if filteredTasks.length === 0}
  <div class="empty-state">
    <Icon name="clipboard-list" />
    <p>
      {searchQuery || statusFilter !== 'all'
        ? 'No tasks match your filters.'
        : appState.currentProjectId
          ? 'No tasks in this project.'
          : 'No tasks found. Select a project to see its tasks.'}
    </p>
  </div>
{:else}
  <div class="task-count-bar" role="status" aria-live="polite">
    Showing {filteredTasks.length} of {tasks.length} tasks
  </div>

  <ul class="task-list" aria-label="Tasks">
    {#each filteredTasks as task ((task.id ?? task.task_id))}
      {@const tid = task.id ?? task.task_id}
      {@const isExpanded = expandedId === tid}
      {@const isActive = task.status === 'in_progress' || task.status === 'running'}
      {@const isFailed = task.status === 'failed'}
      {@const isDone = task.status === 'completed' || task.status === 'complete'}

      <li class="task-item" class:active={isActive} class:failed={isFailed}>
        <div class="task-main">
          <button
            class="task-expand-btn"
            onclick={() => { expandedId = expandedId === tid ? null : tid; }}
            aria-expanded={isExpanded}
            aria-label="Toggle task details: {task.name ?? task.description ?? tid}"
          >
            <span class="agent-icon" aria-hidden="true">
              <Icon name={agentIcon(task.agent)} />
            </span>
          </button>

          <div class="task-info">
            <div class="task-title-row">
              <span class="task-name">{task.name ?? task.description ?? tid}</span>
              <span class="status-badge status-{statusColor(task.status)}">
                {statusLabel(task.status)}
              </span>
            </div>
            <div class="task-meta">
              {#if task.type}
                <span class="task-meta-item">
                  <Icon name="tag" /> {task.type}
                </span>
              {/if}
              {#if task.agent}
                <span class="task-meta-item">
                  <Icon name={agentIcon(task.agent)} /> {task.agent}
                </span>
              {/if}
              {#if task.created_at}
                <span class="task-meta-item">
                  <Icon name="clock" /> {fmt.relativeTime(task.created_at)}
                </span>
              {/if}
              {#if task.duration_ms != null}
                <span class="task-meta-item">
                  <Icon name="stopwatch" /> {fmt.duration(task.duration_ms)}
                </span>
              {/if}
            </div>
          </div>

          <div class="task-actions">
            {#if isFailed || isDone}
              <button
                class="btn btn-secondary btn-xs"
                onclick={() => onRerun(tid)}
                disabled={actionPending || !appState.currentProjectId}
                aria-label="Rerun task {task.name ?? tid}"
                title="Rerun"
              >
                <Icon name="redo-alt" />
              </button>
            {/if}
            {#if isActive || task.status === 'pending'}
              <button
                class="btn btn-danger btn-xs"
                onclick={() => onCancel(tid)}
                disabled={actionPending || !appState.currentProjectId}
                aria-label="Cancel task {task.name ?? tid}"
                title="Cancel"
              >
                <Icon name="times" />
              </button>
            {/if}
          </div>
        </div>

        {#if isExpanded}
          <div class="task-detail" role="region" aria-label="Task details: {task.name ?? tid}">
            {#if task.description && task.description !== task.name}
              <p class="detail-desc">{task.description}</p>
            {/if}

            <dl class="detail-grid">
              <dt>ID</dt>
              <dd class="mono">{tid}</dd>
              {#if task.inputs?.length > 0}
                <dt>Inputs</dt>
                <dd>{task.inputs.join(', ')}</dd>
              {/if}
              {#if task.outputs?.length > 0}
                <dt>Outputs</dt>
                <dd>{task.outputs.join(', ')}</dd>
              {/if}
              {#if task.dependencies?.length > 0}
                <dt>Depends on</dt>
                <dd>{task.dependencies.join(', ')}</dd>
              {/if}
              {#if task.tokens_used != null}
                <dt>Tokens</dt>
                <dd>{fmt.integer(task.tokens_used)}</dd>
              {/if}
            </dl>

            {#if task.error}
              <div class="task-error" role="alert" aria-label="Task error">
                <Icon name="exclamation-triangle" />
                <span>{task.error}</span>
              </div>
            {/if}

            {#if task.output_preview}
              <div class="task-output-preview">
                <span class="preview-label">Output preview</span>
                <pre class="preview-pre">{String(task.output_preview).slice(0, 500)}{String(task.output_preview).length > 500 ? '...' : ''}</pre>
              </div>
            {/if}
          </div>
        {/if}
      </li>
    {/each}
  </ul>
{/if}

<style>
  .filter-bar {
    display: flex;
    gap: 12px;
    margin-bottom: 18px;
    align-items: center;
    flex-wrap: wrap;
  }

  .search-wrap {
    position: relative;
    flex: 1;
    min-width: 200px;
  }

  .search-icon {
    position: absolute;
    left: 10px;
    top: 50%;
    transform: translateY(-50%);
    color: var(--text-muted);
    pointer-events: none;
    font-size: 0.8125rem;
  }

  .input {
    background: var(--surface-elevated);
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

  .search-input { padding-left: 32px; }

  .status-filters {
    display: flex;
    gap: 4px;
    flex-wrap: wrap;
  }

  .filter-btn {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 5px 10px;
    background: none;
    border: 1px solid var(--border-default);
    border-radius: var(--radius-full);
    font-size: 0.8125rem;
    font-weight: 500;
    font-family: inherit;
    color: var(--text-muted);
    cursor: pointer;
    min-height: 44px;
    transition: background var(--transition-base), color var(--transition-base);
  }

  .filter-btn:hover { background: var(--surface-hover); color: var(--text-primary); }
  .filter-btn.active { background: var(--primary-muted); color: var(--primary); border-color: var(--primary); }

  .filter-count {
    background: currentColor;
    color: var(--surface-bg);
    border-radius: var(--radius-full);
    padding: 0 5px;
    font-size: 0.6875rem;
    line-height: 1.4;
  }

  .loading-state,
  .error-state {
    display: flex;
    align-items: center;
    gap: 10px;
    color: var(--text-muted);
    padding: 48px 24px;
  }

  .error-state {
    color: var(--danger);
  }

  .error-state p { margin: 0; }

  .empty-state {
    text-align: center;
    padding: 64px 24px;
    color: var(--text-muted);
    font-size: 0.9375rem;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 10px;
  }

  .empty-state i { font-size: 2rem; opacity: 0.35; }
  .empty-state p { margin: 0; }

  .task-count-bar {
    font-size: 0.8125rem;
    color: var(--text-muted);
    margin-bottom: 10px;
  }

  .task-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .task-item {
    background: var(--surface-elevated);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-md);
    overflow: hidden;
    transition: border-color var(--transition-base);
  }

  .task-item:hover { border-color: var(--border-strong); }
  .task-item.active { border-left: 3px solid var(--primary); }
  .task-item.failed { border-left: 3px solid var(--danger); }

  .task-main {
    display: flex;
    align-items: flex-start;
    gap: 10px;
    padding: 10px 14px;
  }

  .task-expand-btn {
    background: none;
    border: none;
    cursor: pointer;
    min-width: 44px;
    min-height: 44px;
    padding: 2px;
    flex-shrink: 0;
  }

  .agent-icon {
    width: 32px;
    height: 32px;
    background: var(--surface-hover);
    border-radius: var(--radius-sm);
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--text-muted);
    font-size: 0.875rem;
  }

  .task-info {
    flex: 1;
    min-width: 0;
  }

  .task-title-row {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
    margin-bottom: 4px;
  }

  .task-name {
    font-weight: 500;
    font-size: 0.9375rem;
    color: var(--text-primary);
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .task-meta {
    display: flex;
    gap: 10px;
    flex-wrap: wrap;
  }

  .task-meta-item {
    font-size: 0.75rem;
    color: var(--text-muted);
    display: flex;
    align-items: center;
    gap: 4px;
  }

  .task-actions {
    display: flex;
    gap: 6px;
    flex-shrink: 0;
    align-items: flex-start;
  }

  .task-detail {
    padding: 10px 14px 14px;
    border-top: 1px solid var(--border-subtle);
    background: var(--surface-bg);
  }

  .detail-desc {
    font-size: 0.875rem;
    color: var(--text-secondary);
    margin: 0 0 10px 0;
    line-height: var(--leading-relaxed);
  }

  .detail-grid {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: 5px 12px;
    margin: 0 0 10px 0;
    font-size: 0.8125rem;
  }

  .detail-grid dt { color: var(--text-muted); }
  .detail-grid dd { margin: 0; color: var(--text-primary); word-break: break-word; }

  .mono {
    font-family: var(--font-mono);
    font-size: 0.75rem;
  }

  .task-error {
    display: flex;
    align-items: flex-start;
    gap: 8px;
    background: var(--danger-muted);
    border: 1px solid rgba(240, 98, 98, 0.25);
    border-radius: var(--radius-sm);
    padding: 8px 10px;
    font-size: 0.8125rem;
    color: var(--danger);
    margin-top: 8px;
  }

  .task-output-preview { margin-top: 10px; }

  .preview-label {
    display: block;
    font-size: 0.75rem;
    font-weight: 500;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin-bottom: 4px;
  }

  .preview-pre {
    font-family: var(--font-mono);
    font-size: 0.75rem;
    background: var(--surface-hover);
    border-radius: var(--radius-sm);
    padding: 8px 10px;
    overflow-x: auto;
    color: var(--text-secondary);
    white-space: pre-wrap;
    word-break: break-word;
    margin: 0;
    max-height: 200px;
    overflow-y: auto;
  }

  .status-badge {
    display: inline-flex;
    align-items: center;
    padding: 2px 8px;
    border-radius: var(--radius-full);
    font-size: 0.6875rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    flex-shrink: 0;
  }

  .status-primary { background: var(--primary-muted); color: var(--primary); }
  .status-success { background: var(--success-muted); color: var(--success); }
  .status-warning { background: var(--warning-muted); color: var(--warning); }
  .status-danger { background: var(--danger-muted); color: var(--danger); }
  .status-muted { background: var(--surface-hover); color: var(--text-muted); }

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
    min-height: 44px;
    transition: background var(--transition-base);
  }

  .btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .btn-xs {
    padding: 4px 8px;
    font-size: 0.75rem;
  }

  .btn-secondary { background: var(--surface-hover); color: var(--text-primary); border: 1px solid var(--border-default); }
  .btn-secondary:hover:not(:disabled) { background: var(--surface-pressed); }
  .btn-danger { background: var(--danger-muted); color: var(--danger); border: 1px solid rgba(240, 98, 98, 0.3); }
  .btn-danger:hover:not(:disabled) { background: rgba(240, 98, 98, 0.2); }

  @media (max-width: 640px) {
    .filter-bar {
      flex-direction: column;
      align-items: stretch;
    }
  }
</style>
