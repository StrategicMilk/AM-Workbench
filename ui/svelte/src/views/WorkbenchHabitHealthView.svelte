<script>
  import { HabitHealthPanel, HabitHealthStore } from '$lib/components/workbench/habit_health';
  import HelpPopover from '$lib/components/help/HelpPopover.svelte';
  import HelpTooltip from '$lib/components/help/HelpTooltip.svelte';

  let { projectId = new URLSearchParams(window.location.search).get('project_id') ?? 'default' } = $props();
  let store = $derived(new HabitHealthStore(projectId));

  $effect(() => {
    void store.load();
  });
</script>

<main class="habit-health-view" aria-label="Workbench habit health tracker" data-testid="workbench-habit-health">
  <header class="surface-header">
    <div>
      <h1>Habit Health</h1>
      <p>Agent rhythm and routine health for project {projectId}.</p>
      <HelpPopover
        title="Habit health"
        body="Tracks routine completion, check-in cadence, missed sessions, and fatigue risk for long-running agent rhythms. Privacy scope: data is project-scoped and is not shared outside this workbench instance. Non-medical disclaimer: fatigue risk and rhythm metrics are operational indicators only — they reflect agent run patterns, not personal health. Long-running agent rhythm: agents with recurring schedule tasks accumulate rhythm data over time; high missed count or fatigue risk indicates the schedule may need adjustment. Delete removes all rhythm data for a routine. Export downloads a JSON snapshot of all check-in and routine history."
        severity="info"
      />
    </div>
    <span class="boundary-pill">non-medical</span>
  </header>

  {#if store.error}
    <section class="state error" role="alert">{store.error}</section>
  {:else if store.loading}
    <section class="state" role="status">Loading habit rhythm.</section>
  {:else}
    <section class="metric-strip" aria-label="Habit summary">
      <div><span>Routines</span><strong>{store.summary?.routine_count ?? 0}</strong></div>
      <div><span>Check-ins</span><strong>{store.summary?.check_in_count ?? 0}</strong></div>
      <div><span>Missed</span><strong>{store.summary?.rhythm?.missed_count ?? 0}</strong></div>
      <div><span>Fatigue</span><strong>{store.summary?.rhythm?.fatigue_risk ?? 'unknown'}</strong></div>
    </section>
    <HabitHealthPanel {store} />
  {/if}
</main>

<style>
  .habit-health-view {
    display: grid;
    gap: 14px;
    min-height: 100%;
    padding: 18px;
    color: var(--text-primary, #e5e7eb);
    background: var(--bg-primary, #0b1120);
  }

  .surface-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 12px;
  }

  h1,
  p {
    margin: 0;
    letter-spacing: 0;
  }

  h1 {
    font-size: 1.35rem;
  }

  p,
  span {
    color: var(--text-muted, #94a3b8);
  }

  .boundary-pill {
    border: 1px solid #38bdf8;
    border-radius: 999px;
    padding: 4px 8px;
    color: #7dd3fc;
    font-size: 0.78rem;
  }

  .metric-strip,
  :global(.habit-health-panel) {
    display: grid;
    gap: 10px;
  }

  .metric-strip {
    grid-template-columns: repeat(4, minmax(0, 1fr));
  }

  .metric-strip > div,
  :global(.panel) {
    border: 1px solid var(--border-default, #334155);
    border-radius: 8px;
    background: var(--surface-elevated, #111827);
    padding: 12px;
  }

  :global(.habit-health-panel) {
    grid-template-columns: repeat(4, minmax(0, 1fr));
    align-items: start;
  }

  :global(.panel) {
    display: grid;
    gap: 10px;
    min-width: 0;
  }

  :global(label) {
    display: grid;
    gap: 4px;
  }

  :global(input) {
    min-width: 0;
    border: 1px solid var(--border-default, #334155);
    border-radius: 6px;
    background: var(--bg-primary, #0b1120);
    color: var(--text-primary, #e5e7eb);
    padding: 7px 8px;
  }

  :global(button) {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
    border: 1px solid var(--border-default, #334155);
    border-radius: 6px;
    background: var(--surface-hover, #1f2937);
    color: var(--text-primary, #e5e7eb);
    padding: 8px 10px;
  }

  :global(.actions) {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }

  :global(article.denied) {
    border-left: 3px solid #f59e0b;
    padding-left: 8px;
  }

  @media (max-width: 1040px) {
    .metric-strip,
    :global(.habit-health-panel) {
      grid-template-columns: 1fr;
    }
  }
</style>
