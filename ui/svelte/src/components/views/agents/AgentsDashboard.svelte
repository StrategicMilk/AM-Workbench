<script>
  import Icon from '$lib/a11y/Icon.svelte';
  import * as fmt from '$lib/utils/format.js';

  let {
    agentStatus = null,
    activeAgents = [],
    agentTasks = [],
    agentMemory = [],
    pendingDecisions = [],
    actionPending = false,
    onApprove,
    onReject,
  } = $props();

  let expandedAgent = $state(null);
  let expandedDecision = $state(null);
  const MAX_LIST_ITEMS = 50;
  const MAX_DECISIONS = 25;
  const MAX_OPTIONS = 12;

  function boundedArray(values, maxItems = MAX_LIST_ITEMS) {
    if (!Array.isArray(values)) return [];
    return values.slice(0, maxItems);
  }

  const PIPELINE = [
    { role: 'foreman', icon: 'hard-hat', label: 'Foreman', desc: 'Plans and decomposes tasks' },
    { role: 'worker', icon: 'tools', label: 'Worker', desc: 'Executes implementation tasks' },
    { role: 'inspector', icon: 'search', label: 'Inspector', desc: 'Validates and reviews output' },
  ];

  let visibleAgents = $derived(boundedArray(activeAgents));
  let visibleTasks = $derived(boundedArray(agentTasks));
  let visibleMemory = $derived(boundedArray(agentMemory));
  let visibleDecisions = $derived(boundedArray(pendingDecisions, MAX_DECISIONS));

  let agentsByRole = $derived(
    visibleAgents.reduce((acc, agent) => {
      const role = agent.role ?? agent.type ?? 'unknown';
      acc[role] = [...(acc[role] ?? []), agent];
      return acc;
    }, {})
  );

  function pipelineAgentStatus(role) {
    const roleAgents = agentsByRole[role] ?? [];
    if (roleAgents.some((agent) => agent.state === 'busy' || agent.state === 'working')) return 'active';
    if (roleAgents.some((agent) => agent.state === 'idle')) return 'idle';
    if (agentStatus?.initialized) return 'idle';
    return 'offline';
  }

  function pipelineStatusColor(status) {
    if (status === 'active') return 'success';
    if (status === 'idle') return 'primary';
    return 'muted';
  }

  function taskStatusColor(status) {
    const map = { completed: 'success', in_progress: 'primary', failed: 'danger', pending: 'warning' };
    return map[status ?? 'pending'] ?? 'muted';
  }
</script>

<section class="card pipeline-card" aria-label="Three-agent pipeline">
  <h3 class="section-title">
    <Icon name="sitemap" />
    Factory Pipeline
  </h3>
  <div class="pipeline" role="list" aria-label="Agent pipeline stages">
    {#each PIPELINE as agent, i (agent.role)}
      {@const pStatus = pipelineAgentStatus(agent.role)}
      <div class="pipeline-stage" role="listitem" aria-label="{agent.label} agent: {pStatus}">
        <div class="pipeline-node status-ring-{pipelineStatusColor(pStatus)}">
          <Icon name={agent.icon} />
        </div>
        <div class="pipeline-info">
          <span class="pipeline-label">{agent.label}</span>
          <span class="pipeline-desc">{agent.desc}</span>
          <span class="status-dot status-{pipelineStatusColor(pStatus)}" aria-label="Status: {pStatus}">
            {pStatus}
          </span>
        </div>
      </div>
      {#if i < PIPELINE.length - 1}
        <div class="pipeline-arrow" aria-hidden="true">
          <Icon name="arrow-right" />
        </div>
      {/if}
    {/each}
  </div>

  {#if agentStatus}
    <dl class="status-summary">
      <dt>Initialized</dt>
      <dd>{agentStatus.initialized ? 'Yes' : 'No'}</dd>
      <dt>Active tasks</dt>
      <dd>{fmt.integer(agentStatus.active_tasks ?? 0)}</dd>
      <dt>Queue depth</dt>
      <dd>{fmt.integer(agentStatus.queue_depth ?? 0)}</dd>
      {#if agentStatus.uptime_s != null}
        <dt>Uptime</dt>
        <dd>{fmt.duration(agentStatus.uptime_s * 1000)}</dd>
      {/if}
    </dl>
  {/if}
</section>

<div class="agents-grid">
  <section class="card agents-card" aria-label="Active agent instances">
    <h3 class="section-title">
      <Icon name="robot" />
      Active Agents
      <span class="badge">{activeAgents.length}</span>
    </h3>

    {#if visibleAgents.length === 0}
      <div class="empty-state">
        <Icon name="robot" />
        <p>No active agents.</p>
      </div>
    {:else}
      <ul class="agent-list" aria-label="Active agents">
        {#each visibleAgents as agent ((agent.id ?? agent.agent_id))}
          {@const aid = agent.id ?? agent.agent_id}
          <li class="agent-item">
            <button
              class="agent-item-header"
              onclick={() => { expandedAgent = expandedAgent === aid ? null : aid; }}
              aria-expanded={expandedAgent === aid}
              aria-label="Toggle agent {aid}"
            >
              <span class="agent-role-icon">
                <Icon name={PIPELINE.find((item) => item.role === (agent.role ?? agent.type))?.icon ?? 'robot'} />
              </span>
              <span class="agent-id">{agent.role ?? agent.type ?? 'agent'} &mdash; {aid}</span>
              <span class="status-badge status-{taskStatusColor(agent.state)}">
                {agent.state ?? 'unknown'}
              </span>
            </button>
            {#if expandedAgent === aid}
              <div class="agent-detail" role="region" aria-label="Agent {aid} details">
                <dl class="detail-list">
                  <dt>Task</dt>
                  <dd>{agent.current_task ?? agent.task ?? '—'}</dd>
                  <dt>Started</dt>
                  <dd>{fmt.relativeTime(agent.started_at)}</dd>
                  {#if agent.model}
                    <dt>Model</dt>
                    <dd>{agent.model}</dd>
                  {/if}
                </dl>
              </div>
            {/if}
          </li>
        {/each}
      </ul>
    {/if}
  </section>

  <section class="card tasks-card" aria-label="Agent task queue">
    <h3 class="section-title">
      <Icon name="list-ol" />
      Task Queue
      <span class="badge">{agentTasks.length}</span>
    </h3>

    {#if visibleTasks.length === 0}
      <div class="empty-state">
        <Icon name="inbox" />
        <p>No tasks in queue.</p>
      </div>
    {:else}
      <ul class="task-list" aria-label="Agent tasks">
        {#each visibleTasks as task ((task.id ?? task.task_id))}
          <li class="task-item">
            <div class="task-row">
              <span class="task-name">{task.name ?? task.description ?? task.id ?? 'unnamed'}</span>
              <span class="status-badge status-{taskStatusColor(task.status)}">
                {task.status ?? 'pending'}
              </span>
            </div>
            <div class="task-meta">
              {#if task.agent}
                <span class="task-meta-item">
                  <Icon name="robot" /> {task.agent}
                </span>
              {/if}
              {#if task.created_at}
                <span class="task-meta-item">
                  <Icon name="clock" /> {fmt.relativeTime(task.created_at)}
                </span>
              {/if}
            </div>
          </li>
        {/each}
      </ul>
    {/if}
  </section>

  <section class="card memory-card" aria-label="Shared agent memory">
    <h3 class="section-title">
      <Icon name="database" />
      Shared Memory
      <span class="badge">{agentMemory.length}</span>
    </h3>

    {#if visibleMemory.length === 0}
      <div class="empty-state">
        <Icon name="database" />
        <p>No shared memory entries.</p>
      </div>
    {:else}
      <ul class="memory-list" aria-label="Shared memory entries">
        {#each visibleMemory as entry ((entry.id ?? entry.key ?? entry))}
          <li class="memory-item">
            <span class="memory-key">{entry.key ?? entry.type ?? 'entry'}</span>
            <span class="memory-value">{
              typeof entry.value === 'object'
                ? JSON.stringify(entry.value).slice(0, 60)
                : String(entry.value ?? entry.content ?? '').slice(0, 60)
            }</span>
          </li>
        {/each}
      </ul>
    {/if}
  </section>
</div>

{#if visibleDecisions.length > 0}
  <section class="card decisions-card" aria-label="Pending decisions requiring approval">
    <h3 class="section-title decisions-title">
      <Icon name="exclamation-triangle" />
      Pending Decisions
      <span class="badge badge-warning">{pendingDecisions.length}</span>
    </h3>

    <ul class="decision-list" aria-label="Pending agent decisions">
      {#each visibleDecisions as decision ((decision.id ?? decision.decision_id))}
        {@const did = decision.id ?? decision.decision_id}
        <li class="decision-item">
          <div class="decision-header">
            <div class="decision-meta">
              <span class="decision-type status-badge status-warning">
                {decision.type ?? 'decision'}
              </span>
              <button
                class="decision-title-btn"
                onclick={() => { expandedDecision = expandedDecision === did ? null : did; }}
                aria-expanded={expandedDecision === did}
                aria-label="Toggle decision details: {decision.prompt ?? did}"
              >
                {decision.prompt ?? did}
              </button>
            </div>
            <div class="decision-actions">
              <span class="decision-age">{fmt.relativeTime(decision.created_at)}</span>
              <button
                class="btn btn-success btn-sm"
                onclick={() => onApprove(did, {})}
                disabled={actionPending}
                aria-label="Approve decision: {decision.prompt ?? did}"
              >
                <Icon name="check" /> Approve
              </button>
              <button
                class="btn btn-danger btn-sm"
                onclick={() => onReject(did)}
                disabled={actionPending}
                aria-label="Reject decision: {decision.prompt ?? did}"
              >
                <Icon name="times" /> Reject
              </button>
            </div>
          </div>
          {#if expandedDecision === did && (decision.description ?? decision.context)}
            <div class="decision-body" role="region" aria-label="Decision details">
              <p class="decision-desc">{decision.description ?? decision.context}</p>
              {#if decision.options?.length > 0}
                <ul class="decision-options" aria-label="Decision options">
                  {#each boundedArray(decision.options, MAX_OPTIONS) as opt, idx (idx)}
                    <li>{opt}</li>
                  {/each}
                </ul>
              {/if}
            </div>
          {/if}
        </li>
      {/each}
    </ul>
  </section>
{/if}

<style>
  .card {
    background: var(--surface-elevated);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-lg);
    padding: 16px;
    margin-bottom: 16px;
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

  .pipeline {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
    margin-bottom: 16px;
  }

  .pipeline-stage {
    display: flex;
    align-items: center;
    gap: 12px;
    flex: 1;
    min-width: 160px;
    background: var(--surface-bg);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-md);
    padding: 12px;
  }

  .pipeline-node {
    width: 44px;
    height: 44px;
    border-radius: var(--radius-full);
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 1.125rem;
    flex-shrink: 0;
    border: 2px solid transparent;
  }

  .status-ring-success { background: var(--success-muted); color: var(--success); border-color: var(--success); }
  .status-ring-primary { background: var(--primary-muted); color: var(--primary); border-color: var(--primary); }
  .status-ring-muted { background: var(--surface-hover); color: var(--text-muted); border-color: var(--border-default); }

  .pipeline-info {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }

  .pipeline-label {
    font-weight: 600;
    font-size: 0.9375rem;
    color: var(--text-primary);
  }

  .pipeline-desc {
    font-size: 0.75rem;
    color: var(--text-muted);
  }

  .pipeline-arrow {
    color: var(--text-muted);
    font-size: 0.875rem;
    flex-shrink: 0;
  }

  .status-dot {
    font-size: 0.6875rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    display: inline-flex;
    align-items: center;
    gap: 4px;
  }

  .status-dot::before {
    content: '';
    width: 6px;
    height: 6px;
    border-radius: var(--radius-full);
    background: currentColor;
    display: inline-block;
  }

  .status-success { color: var(--success); }
  .status-primary { color: var(--primary); }
  .status-warning { color: var(--warning); }
  .status-danger { color: var(--danger); }
  .status-muted { color: var(--text-muted); }

  .status-summary {
    display: flex;
    flex-wrap: wrap;
    gap: 6px 20px;
    margin: 0;
    font-size: 0.8125rem;
    border-top: 1px solid var(--border-subtle);
    padding-top: 12px;
  }

  .status-summary dt { color: var(--text-muted); }
  .status-summary dd { color: var(--text-primary); margin: 0; font-weight: 500; }

  .agents-grid {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 16px;
    margin-bottom: 16px;
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

  .status-badge.status-success { background: var(--success-muted); color: var(--success); }
  .status-badge.status-primary { background: var(--primary-muted); color: var(--primary); }
  .status-badge.status-warning { background: var(--warning-muted); color: var(--warning); }
  .status-badge.status-danger { background: var(--danger-muted); color: var(--danger); }
  .status-badge.status-muted { background: var(--surface-hover); color: var(--text-muted); }

  .badge {
    background: var(--surface-hover);
    color: var(--text-muted);
    border-radius: var(--radius-full);
    padding: 1px 8px;
    font-size: 0.75rem;
    font-weight: 600;
  }

  .badge-warning { background: var(--warning-muted); color: var(--warning); }

  .agent-list,
  .task-list,
  .memory-list,
  .decision-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
  }

  .agent-list,
  .task-list,
  .memory-list {
    gap: 6px;
  }

  .agent-item {
    background: var(--surface-bg);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    overflow: hidden;
  }

  .agent-item-header {
    width: 100%;
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 10px;
    background: none;
    border: none;
    cursor: pointer;
    font-family: inherit;
    text-align: left;
  }

  .agent-item-header:hover { background: var(--surface-hover); }

  .agent-role-icon {
    width: 24px;
    height: 24px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: var(--primary-muted);
    color: var(--primary);
    border-radius: var(--radius-sm);
    font-size: 0.75rem;
    flex-shrink: 0;
  }

  .agent-id {
    flex: 1;
    font-size: 0.8125rem;
    color: var(--text-primary);
    font-family: var(--font-mono);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .agent-detail {
    padding: 8px 10px;
    border-top: 1px solid var(--border-subtle);
  }

  .detail-list {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: 4px 10px;
    margin: 0;
    font-size: 0.75rem;
  }

  .detail-list dt { color: var(--text-muted); }
  .detail-list dd { margin: 0; color: var(--text-primary); word-break: break-word; }

  .task-item {
    background: var(--surface-bg);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    padding: 8px 10px;
  }

  .task-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    margin-bottom: 4px;
  }

  .task-name {
    font-size: 0.8125rem;
    color: var(--text-primary);
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .task-meta {
    display: flex;
    gap: 10px;
  }

  .task-meta-item {
    font-size: 0.75rem;
    color: var(--text-muted);
    display: flex;
    align-items: center;
    gap: 4px;
  }

  .memory-item {
    display: flex;
    gap: 8px;
    padding: 6px 8px;
    background: var(--surface-bg);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    font-size: 0.75rem;
  }

  .memory-key {
    color: var(--primary);
    font-family: var(--font-mono);
    flex-shrink: 0;
    font-weight: 500;
  }

  .memory-value {
    color: var(--text-muted);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .decisions-title { color: var(--warning); }
  .decisions-title i { color: var(--warning); }

  .decision-list {
    gap: 10px;
  }

  .decision-item {
    background: var(--warning-muted);
    border: 1px solid rgba(245, 165, 36, 0.25);
    border-radius: var(--radius-md);
    overflow: hidden;
  }

  .decision-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 10px 14px;
    gap: 10px;
    flex-wrap: wrap;
  }

  .decision-meta {
    display: flex;
    align-items: center;
    gap: 8px;
    flex: 1;
    min-width: 0;
  }

  .decision-title-btn {
    background: none;
    border: none;
    cursor: pointer;
    font-size: 0.9375rem;
    font-weight: 500;
    color: var(--text-primary);
    font-family: inherit;
    text-align: left;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .decision-actions {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-shrink: 0;
  }

  .decision-age {
    font-size: 0.75rem;
    color: var(--text-muted);
  }

  .decision-body {
    padding: 10px 14px;
    border-top: 1px solid rgba(245, 165, 36, 0.2);
  }

  .decision-desc {
    font-size: 0.875rem;
    color: var(--text-secondary);
    margin: 0 0 8px 0;
    line-height: var(--leading-relaxed);
  }

  .decision-options {
    margin: 0;
    padding-left: 20px;
    font-size: 0.8125rem;
    color: var(--text-muted);
  }

  .decision-options li { margin-bottom: 4px; }

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
  .btn-success { background: var(--success-muted); color: var(--success); border: 1px solid rgba(56, 211, 159, 0.3); }
  .btn-success:hover:not(:disabled) { background: rgba(56, 211, 159, 0.2); }
  .btn-danger { background: var(--danger-muted); color: var(--danger); border: 1px solid rgba(240, 98, 98, 0.3); }
  .btn-danger:hover:not(:disabled) { background: rgba(240, 98, 98, 0.2); }

  .empty-state {
    text-align: center;
    padding: 24px;
    color: var(--text-muted);
    font-size: 0.875rem;
  }

  .empty-state i {
    font-size: 1.5rem;
    margin-bottom: 6px;
    display: block;
    opacity: 0.35;
  }

  @media (max-width: 1024px) {
    .agents-grid { grid-template-columns: 1fr 1fr; }
  }

  @media (max-width: 640px) {
    .agents-grid { grid-template-columns: 1fr; }
    .pipeline { flex-direction: column; }
    .pipeline-arrow { transform: rotate(90deg); }
  }
</style>
