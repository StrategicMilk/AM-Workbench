<script>
  import { requireEvidence } from '$lib/evidence/evidenceGuard.js';
  import { managedAgentsStore } from './store.svelte.js';

  let { projectId = 'default' } = $props();

  let agents = $derived(managedAgentsStore.snapshot?.agents ?? []);
  let dependencyContracts = $derived(managedAgentsStore.snapshot?.dependency_contracts ?? []);
  let status = $derived(managedAgentsStore.snapshot?.status ?? 'unknown');
  let degradationReasons = $derived(managedAgentsStore.snapshot?.degradation_reasons ?? []);
  let attentionIds = $derived(managedAgentsStore.snapshot?.user_intervention?.attention_agent_ids ?? []);
  let actionError = $state('');

  function toolLabel(agent) {
    return Array.isArray(agent.requested_tools) ? agent.requested_tools.join(', ') : 'unknown';
  }

  function memoryLabel(agent) {
    return Array.isArray(agent.memory_scope) ? agent.memory_scope.join(', ') : 'unknown';
  }

  function evidenceRefsForAgent(agent) {
    return [
      ...(Array.isArray(agent?.evidence_refs) ? agent.evidence_refs : []),
      agent?.cost_ceiling_ref,
      agent?.dependencies?.route_ledger_ref,
      agent?.dependencies?.trace_eval_ref,
      agent?.lease_ref,
      agent?.template_ref,
    ].filter(Boolean);
  }

  function agentEvidenceIssue(agent) {
    const refs = evidenceRefsForAgent(agent);
    if (refs.length === 0) {
      return 'missing_managed_agent_evidence';
    }
    try {
      requireEvidence(refs, `managed-agent:${agent?.agent_id ?? 'unknown'}`);
      return '';
    } catch (err) {
      return err.message ?? String(err);
    }
  }

  async function pauseAgent(agent) {
    const issue = agentEvidenceIssue(agent);
    if (issue) {
      actionError = issue;
      return;
    }
    actionError = '';
    await managedAgentsStore.pause(agent.agent_id);
  }

  async function retireAgent(agent) {
    const issue = agentEvidenceIssue(agent);
    if (issue) {
      actionError = issue;
      return;
    }
    actionError = '';
    await managedAgentsStore.retire(agent.agent_id);
  }

  $effect(() => {
    void projectId;
    void managedAgentsStore.refresh();
  });
</script>

<section class="managed-agents-panel" aria-label="Managed agent workspace" data-project-id={projectId}>
  <header class="panel-header">
    <div>
      <h1>Managed Agents</h1>
      <p>Long-running templates, chat agents, watchers, automations, memory, leases, and run safety in one workspace.</p>
    </div>
    <button type="button" onclick={managedAgentsStore.refresh} disabled={managedAgentsStore.loading}>
      <i class="fas fa-rotate"></i>
      <span>Refresh</span>
    </button>
  </header>

  <section class="status-strip" aria-label="Workspace status">
    <span class="status-pill" data-status={status}>{status}</span>
    <span>{agents.length} agents</span>
    <span>{attentionIds.length} need attention</span>
    <span>{dependencyContracts.length} dependencies</span>
  </section>

  {#if managedAgentsStore.loading}
    <section class="empty-state" role="status" aria-live="polite">Loading managed agents...</section>
  {:else if managedAgentsStore.error || actionError}
    <section class="alert" role="alert">
      <strong>Action required</strong>
      <span>{managedAgentsStore.error || actionError}</span>
    </section>
  {:else}
    {#if degradationReasons.length > 0}
      <section class="alert" role="alert" aria-live="assertive">
        <strong>Degraded workspace</strong>
        <ul>
          {#each degradationReasons as reason (reason)}
            <li>{reason}</li>
          {/each}
        </ul>
      </section>
    {/if}

    <section class="agent-grid" aria-label="Managed agents">
      {#each agents as agent (agent.agent_id)}
        {@const evidenceIssue = agentEvidenceIssue(agent)}
        <article class="agent-row" data-state={agent.state}>
          <div class="agent-main">
            <div>
              <h2>{agent.display_name}</h2>
              <p>{agent.purpose}</p>
            </div>
            <span class="state-pill">{agent.state}</span>
          </div>

          <dl class="agent-facts">
            <div>
              <dt>Template</dt>
              <dd>{agent.template_id}</dd>
            </div>
            <div>
              <dt>Tools</dt>
              <dd>{toolLabel(agent)}</dd>
            </div>
            <div>
              <dt>Memory</dt>
              <dd>{memoryLabel(agent)}</dd>
            </div>
            <div>
              <dt>Cost</dt>
              <dd>{agent.cost_ceiling_ref}</dd>
            </div>
            <div>
              <dt>Route</dt>
              <dd>{agent.dependencies?.route_ledger_ref}</dd>
            </div>
            <div>
              <dt>Trace</dt>
              <dd>{agent.dependencies?.trace_eval_ref}</dd>
            </div>
          </dl>

          <div class="intervention-bar" aria-label="User intervention controls">
            <span>{evidenceIssue || agent.intervention?.why_running}</span>
            <div>
              <button type="button" onclick={() => pauseAgent(agent)} disabled={agent.state !== 'active' || Boolean(evidenceIssue)}>
                <i class="fas fa-pause"></i>
                <span>Pause</span>
              </button>
              <button type="button" onclick={() => retireAgent(agent)} disabled={agent.state === 'retired' || Boolean(evidenceIssue)}>
                <i class="fas fa-box-archive"></i>
                <span>Retire</span>
              </button>
            </div>
          </div>
        </article>
      {:else}
        <section class="empty-state">
          No managed agents are installed for this project.
        </section>
      {/each}
    </section>

    <section class="dependency-list" aria-label="Composed dependency contracts">
      <h2>Dependency Contracts</h2>
      <div>
        {#each dependencyContracts as dependency (dependency.surface)}
          <span>{dependency.surface}</span>
        {/each}
      </div>
    </section>
  {/if}
</section>

<style>
  .managed-agents-panel {
    display: grid;
    gap: 16px;
    min-height: 100%;
    padding: 18px;
    background: var(--bg-primary, #0b1120);
    color: var(--text-primary, #e5e7eb);
  }

  .panel-header,
  .agent-main,
  .intervention-bar {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 14px;
  }

  h1,
  h2,
  p {
    margin-top: 0;
  }

  h1 {
    margin-bottom: 4px;
    font-size: 28px;
    letter-spacing: 0;
  }

  h2 {
    margin-bottom: 4px;
    font-size: 17px;
    letter-spacing: 0;
  }

  p {
    margin-bottom: 0;
    color: var(--text-muted, #94a3b8);
  }

  button {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    border: 1px solid var(--border-default, #334155);
    border-radius: 6px;
    background: var(--surface-elevated, #111827);
    color: inherit;
    padding: 8px 11px;
  }

  button:disabled {
    cursor: not-allowed;
    opacity: 0.55;
  }

  .status-strip,
  .dependency-list div {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
  }

  .status-strip span,
  .dependency-list span,
  .empty-state,
  .alert {
    border: 1px solid var(--border-default, #334155);
    border-radius: 8px;
    background: var(--surface-elevated, #111827);
    padding: 10px;
  }

  .status-pill[data-status="ready"] {
    color: #86efac;
  }

  .status-pill[data-status="degraded"],
  .status-pill[data-status="recovery_needed"],
  .status-pill[data-status="unknown"] {
    color: #fca5a5;
  }

  .alert {
    border-color: #f59e0b;
  }

  .alert ul {
    margin: 8px 0 0;
  }

  .agent-grid {
    display: grid;
    gap: 12px;
  }

  .agent-row {
    display: grid;
    gap: 12px;
    border: 1px solid var(--border-default, #334155);
    border-radius: 8px;
    background: var(--surface-primary, #0f172a);
    padding: 14px;
  }

  .state-pill {
    border: 1px solid var(--border-default, #334155);
    border-radius: 6px;
    padding: 5px 8px;
    color: #86efac;
    font-size: 12px;
  }

  .agent-row[data-state="paused"] .state-pill {
    color: #fbbf24;
  }

  .agent-row[data-state="retired"] .state-pill {
    color: #fca5a5;
  }

  .agent-facts {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
    gap: 10px;
    margin: 0;
  }

  .agent-facts div {
    min-height: 70px;
    border: 1px solid var(--border-default, #334155);
    border-radius: 8px;
    padding: 10px;
  }

  dt {
    margin-bottom: 6px;
    color: var(--text-muted, #94a3b8);
    font-size: 12px;
  }

  dd {
    margin: 0;
    overflow-wrap: anywhere;
    font-size: 14px;
  }

  .intervention-bar {
    align-items: center;
    border-top: 1px solid var(--border-default, #334155);
    padding-top: 12px;
  }

  .intervention-bar div {
    display: flex;
    gap: 8px;
  }

  .dependency-list {
    display: grid;
    gap: 10px;
  }

  @media (max-width: 800px) {
    .panel-header,
    .agent-main,
    .intervention-bar {
      flex-direction: column;
    }
  }
</style>
