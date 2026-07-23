<script>
  const {
    projectId = 'default',
    snapshot = {},
  } = $props();

  const SECRET_PATTERN = /\b(api[_-]?key|token|secret|password)\b\s*[:=]\s*["']?[^"',\s]+/gi;
  const WINDOWS_PATH_PATTERN = /[A-Za-z]:\\[^\s,;]+/g;

  function redactDisplayText(value) {
    return String(value ?? '')
      .replace(SECRET_PATTERN, '$1=[redacted]')
      .replace(WINDOWS_PATH_PATTERN, '[redacted-path]');
  }

  const safeSnapshot = $derived(snapshot ?? {});
  const frictionMap = $derived(safeSnapshot.friction_map ?? []);
  const automationCandidates = $derived(safeSnapshot.automation_candidates ?? []);
  const preferenceDrafts = $derived(safeSnapshot.preference_drafts ?? []);
  const trustBoundaryMap = $derived(safeSnapshot.trust_boundary_map ?? []);
  const degradedSignals = $derived(safeSnapshot.degraded_signals ?? []);

  const tiles = $derived([
    { label: 'Accepted', value: safeSnapshot.accepted_count ?? 0 },
    { label: 'Degraded', value: safeSnapshot.degraded_count ?? 0 },
    { label: 'Question debt', value: safeSnapshot.question_debt_meter?.open_question_count ?? 0 },
    { label: 'Effort min', value: safeSnapshot.user_effort_accounting?.total_effort_minutes ?? 0 },
  ]);
</script>

<main
  class="user-observability"
  aria-label="Workbench user input observability"
  data-testid="workbench-user-observability"
>
  <header class="surface-header">
    <div>
      <h1>User Observability</h1>
      <p>{redactDisplayText(projectId)}</p>
    </div>
    <span class="policy-pill">local only</span>
  </header>

  <section class="metric-strip" aria-label="Signal summary">
    {#each tiles as tile}
      <div class="metric-tile" role="status" aria-label={`${tile.label}: ${tile.value}`}>
        <span>{tile.label}</span>
        <strong>{tile.value}</strong>
      </div>
    {/each}
  </section>

  <section class="surface-grid" aria-label="User signal dashboards">
    <section class="panel">
      <h2>Friction Map</h2>
      {#each frictionMap as row}
        <article>
          <strong>{redactDisplayText(row.summary)}</strong>
          <span>{Math.round((row.confidence ?? 0) * 100)}% confidence</span>
        </article>
      {:else}
        <article><strong>No accepted friction signals</strong></article>
      {/each}
    </section>

    <section class="panel">
      <h2>Automation Candidates</h2>
      {#each automationCandidates as row}
        <article>
          <strong>{redactDisplayText(row.summary)}</strong>
          <span>{row.activation_allowed ? 'activation available' : row.status}</span>
        </article>
      {:else}
        <article><strong>No automation candidates</strong></article>
      {/each}
    </section>

    <section class="panel">
      <h2>Preference Drafts</h2>
      {#each preferenceDrafts as row}
        <article>
          <strong>{redactDisplayText(row.summary)}</strong>
          <span>{Math.round((row.confidence ?? 0) * 100)}% confidence</span>
        </article>
      {:else}
        <article><strong>No preference drafts</strong></article>
      {/each}
    </section>

    <section class="panel">
      <h2>Trust Boundaries</h2>
      {#each trustBoundaryMap as row}
        <article>
          <strong>{redactDisplayText(row.summary)}</strong>
          <span>approval sensitive</span>
        </article>
      {:else}
        <article><strong>No trusted boundary signals</strong></article>
      {/each}
    </section>

    <section class="panel">
      <h2>Recommendation Quality</h2>
      <article>
        <strong>{safeSnapshot.recommendation_quality_dashboard?.positive_feedback_count ?? 0} positive</strong>
        <span>{safeSnapshot.recommendation_quality_dashboard?.ignored_count ?? 0} ignored recommendations</span>
      </article>
    </section>

    <section class="panel warning">
      <h2>Fail-Closed Signals</h2>
      {#each degradedSignals as row}
        <article role="alert">
          <strong>{redactDisplayText(row.summary)}</strong>
          <span>{redactDisplayText((row.blockers ?? []).join(', '))}</span>
        </article>
      {:else}
        <article role="status"><strong>No degraded signals</strong></article>
      {/each}
    </section>
  </section>
</main>

<style>
  .user-observability {
    display: grid;
    gap: 12px;
    max-width: 1480px;
    padding: 16px;
    color: var(--text-primary, #e5e7eb);
  }

  .surface-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 12px;
  }

  h1,
  h2,
  p {
    margin: 0;
  }

  h1 {
    font-size: 1.25rem;
  }

  h2 {
    font-size: 0.92rem;
  }

  .surface-header p,
  article span,
  .metric-tile span {
    color: var(--text-muted, #94a3b8);
    font-size: 0.8rem;
  }

  .policy-pill {
    border: 1px solid #22c55e;
    border-radius: 999px;
    color: #86efac;
    padding: 4px 8px;
    font-size: 0.78rem;
  }

  .metric-strip,
  .surface-grid {
    display: grid;
    gap: 10px;
  }

  .metric-strip {
    grid-template-columns: repeat(4, minmax(0, 1fr));
  }

  .surface-grid {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }

  .metric-tile,
  .panel {
    border: 1px solid var(--border-default, #334155);
    border-radius: 8px;
    background: var(--surface-elevated, #111827);
    padding: 12px;
  }

  .metric-tile {
    display: grid;
    gap: 6px;
  }

  .metric-tile strong {
    font-size: 1.35rem;
  }

  .panel {
    display: grid;
    gap: 10px;
    min-width: 0;
  }

  .panel.warning {
    border-color: #f59e0b;
  }

  article {
    display: grid;
    gap: 4px;
    min-width: 0;
  }

  article strong {
    font-size: 0.86rem;
    overflow-wrap: anywhere;
  }

  @media (max-width: 980px) {
    .metric-strip,
    .surface-grid {
      grid-template-columns: 1fr;
    }
  }
</style>
