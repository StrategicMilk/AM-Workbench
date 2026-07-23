<script>
  /**
   * Workbench Mission Control view.
   *
   * Mount note: this Vite SPA currently routes top-level views through
   * App.svelte, which is outside this pack's write scope. Wire this view into
   * the shell router in the Wave-12 trailer task named by the pack index.
   */
  import QueueLanes from '$components/workbench/QueueLanes.svelte';
  import QueueLeaseTable from '$components/workbench/QueueLeaseTable.svelte';
  import AgentTaskList from '$components/workbench/AgentTaskList.svelte';
  import AgentEscalations from '$components/workbench/AgentEscalations.svelte';
  import AccessibleStatusRegion from '$components/AccessibleStatusRegion.svelte';
  import { workbenchKernelRequest } from '$lib/api.js';
  import HelpPopover from '$lib/components/help/HelpPopover.svelte';
  import HelpTooltip from '$lib/components/help/HelpTooltip.svelte';

  const MISSION_CONTROL_REFRESH_MS = 10000;

  let { projectId } = $props();

  let snapshot = $state(null);
  let fetchError = $state(null);
  let isFetching = $state(false);
  let pollingStopped = $state(false);
  let isLoading = $derived(snapshot === null && fetchError === null);

  async function loadSnapshot() {
    if (!projectId || isFetching || pollingStopped) return;
    isFetching = true;
    try {
      const body = await workbenchKernelRequest(
        `/api/v1/projects/${encodeURIComponent(projectId)}/mission-control/snapshot`
      );
      snapshot = body;
      fetchError = null;
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      const response = { status: err?.status };
      const body = err?.body ?? {};
      if (response.status === 503 && body.error_kind === 'spine_corrupt') {
        fetchError = 'Workbench spine unavailable - snapshot cannot be rendered. Check server logs.';
        pollingStopped = true;
        return;
      }
      fetchError = `Mission Control snapshot failed: ${message}`;
    } finally {
      isFetching = false;
    }
  }

  function refreshNow() {
    if (isFetching) return;
    void loadSnapshot();
  }

  function tabIsVisible() {
    return typeof document === 'undefined' || document.visibilityState === 'visible';
  }

  function formatTimestamp(value) {
    if (!value) return 'not generated';
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
  }

  $effect(() => {
    if (!projectId || pollingStopped) return;
    void loadSnapshot();
    const refreshWhenVisible = () => {
      if (tabIsVisible()) void loadSnapshot();
    };
    const handleVisibilityChange = () => {
      if (tabIsVisible()) void loadSnapshot();
    };
    const intervalId = setInterval(() => {
      refreshWhenVisible();
    }, MISSION_CONTROL_REFRESH_MS);
    document.addEventListener('visibilitychange', handleVisibilityChange);
    return () => {
      clearInterval(intervalId);
      document.removeEventListener('visibilitychange', handleVisibilityChange);
    };
  });
</script>

<main class="mission-control" aria-label="Mission Control" data-view="mission-control">
  <header class="mission-header">
    <div>
      <h1>Mission Control</h1>
      <p>Scheduler lanes, queue leases, agent tasks, recursive plans, and escalations.</p>
      <HelpPopover
        title="Mission Control"
        body="Real-time view of all durable run objects for this project. Each run has a status (running, succeeded, interrupted, blocked, or recovery_needed) and is backed by a persistent spine record. The Workbench refreshes the snapshot every 10 seconds while this tab is visible; the snapshot timestamp shows when the last successful refresh completed. Scheduler lanes show priority-ordered work queues; queue leases show which agent currently holds a task lock. If a run enters recovery_needed, the Foreman automatically restarts it from the last durable checkpoint — no manual intervention is required unless the run stays in that state for more than two heartbeat cycles (60 seconds)."
        severity="info"
      />
    </div>
    <div class="refresh-box">
      <span data-testid="snapshot-timestamp">
        {snapshot ? formatTimestamp(snapshot.generated_at_utc) : 'Waiting for snapshot'}
      </span>
      <button type="button" aria-label="Refresh mission control snapshot" onclick={refreshNow} disabled={isFetching}>
        Refresh now
      </button>
    </div>
  </header>

  <AccessibleStatusRegion
    critical={Boolean(fetchError)}
    message={fetchError || (isFetching ? 'Refreshing Mission Control snapshot.' : '')}
  />

  {#if fetchError}
    <aside role="alert" class="degraded-banner fatal" aria-live="assertive">{fetchError}</aside>
  {:else if isLoading}
    <section class="loading-state" role="status" aria-live="polite">Loading mission-control snapshot...</section>
  {:else if snapshot}
    {#if snapshot.status === "empty"}
      <section class="empty-project" role="status" aria-live="polite">No active workbench activity for this project.</section>
    {/if}

    {#if snapshot.degraded === true}
      <aside role="alert" class="degraded-banner" aria-live="assertive">
        {snapshot.degraded_reason || 'Mission Control snapshot is degraded.'}
      </aside>
    {/if}

    <section class="mission-grid" aria-label="Mission Control panes">
      <section class="pane lanes-pane" data-pane="lanes" aria-labelledby="mission-lanes-heading">
        <h2 id="mission-lanes-heading">Scheduler Lanes</h2>
        <QueueLanes lanes={snapshot.lanes} />
      </section>

      <section class="pane queue-pane" data-pane="queue" aria-labelledby="mission-queue-heading">
        <h2 id="mission-queue-heading">Queue & Leases</h2>
        <QueueLeaseTable entries={snapshot.queue} {projectId} />
      </section>

      <section class="pane agents-pane" data-pane="agents" aria-labelledby="mission-agents-heading">
        <h2 id="mission-agents-heading">Agent Tasks</h2>
        <AgentTaskList rows={snapshot.agent_tasks} {projectId} />
      </section>

      <section
        class="pane recursive-pane"
        data-pane="recursive-children"
        aria-labelledby="mission-recursive-heading"
      >
        <h2 id="mission-recursive-heading">Recursive Plans</h2>
        {#if snapshot.recursive_children.length === 0}
          <p class="quiet">No recursive child plans for this project.</p>
        {:else}
          <ul class="recursive-list">
            {#each snapshot.recursive_children as link (link.child_plan_id)}
              <li>
                <span>{link.parent_plan_id}</span>
                <span aria-hidden="true">-></span>
                <a href="/projects/{projectId}/plans/{link.child_plan_id}">{link.child_plan_id}</a>
              </li>
            {/each}
          </ul>
          {#if snapshot.recursive_children_truncated_at !== null}
            <p class="quiet">Showing first 200 of {snapshot.recursive_children_truncated_at}</p>
          {/if}
        {/if}
      </section>

      <section class="pane escalations-pane" data-pane="escalations" aria-label="escalations" aria-labelledby="mission-escalations-heading">
        <h2 id="mission-escalations-heading">Escalations</h2>
        <AgentEscalations rows={snapshot.escalations} {projectId} />
      </section>
    </section>
  {/if}
</main>

<style>
  .mission-control {
    display: grid;
    gap: 16px;
    padding: 16px;
    color: var(--text-primary, #e5e7eb);
  }

  .mission-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 16px;
  }

  h1,
  h2,
  p {
    margin-top: 0;
  }

  h1 {
    margin-bottom: 4px;
    font-size: 1.35rem;
  }

  h2 {
    margin-bottom: 12px;
    font-size: 1rem;
  }

  .refresh-box {
    display: flex;
    align-items: center;
    gap: 10px;
    color: var(--text-muted, #94a3b8);
    font-size: 0.82rem;
  }

  button {
    border: 1px solid var(--border-color, #334155);
    border-radius: 6px;
    background: var(--surface-elevated, #111827);
    color: inherit;
    padding: 6px 10px;
  }

  button:focus-visible {
    outline: 2px solid #38bdf8;
    outline-offset: 2px;
  }

  .mission-grid {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(320px, 0.9fr);
    gap: 14px;
  }

  .pane {
    min-width: 0;
    border: 1px solid var(--border-color, #334155);
    border-radius: 8px;
    background: var(--surface-panel, #0f172a);
    padding: 14px;
    overflow: auto;
  }

  .lanes-pane,
  .agents-pane {
    grid-column: 1 / -1;
  }

  .degraded-banner,
  .empty-project,
  .loading-state {
    border-radius: 8px;
    padding: 12px;
  }

  .degraded-banner {
    border: 1px solid #f59e0b;
    background: rgba(120, 53, 15, 0.32);
  }

  .degraded-banner.fatal {
    border-color: #ef4444;
    background: rgba(127, 29, 29, 0.3);
  }

  .empty-project,
  .loading-state {
    border: 1px solid var(--border-color, #334155);
    background: rgba(148, 163, 184, 0.1);
    color: var(--text-muted, #94a3b8);
  }

  .recursive-list {
    display: grid;
    gap: 8px;
    padding-left: 0;
    list-style: none;
  }

  .recursive-list li {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
  }

  .quiet {
    color: var(--text-muted, #94a3b8);
  }

  @media (max-width: 980px) {
    .mission-header,
    .refresh-box {
      align-items: stretch;
      flex-direction: column;
    }

    .mission-grid {
      grid-template-columns: 1fr;
    }
  }
</style>
