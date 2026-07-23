<script>
  let { summary = {} } = $props();

  const rhythm = $derived(summary?.rhythm ?? {});
  const missed = $derived(summary?.missed_routine_ids ?? []);
</script>

<section class="panel rhythm-timeline" aria-label="Habit rhythm timeline">
  <h2>Rhythm</h2>
  <div class="metric-row">
    <span>Streak</span>
    <strong>{rhythm.streak_count ?? 0}</strong>
  </div>
  <div class="metric-row">
    <span>Missed</span>
    <strong>{rhythm.missed_count ?? 0}</strong>
  </div>
  <div class="metric-row">
    <span>Fatigue</span>
    <strong>{rhythm.fatigue_risk ?? 'unknown'}</strong>
  </div>
  <ol>
    {#each missed as routineId}
      <li>{routineId}</li>
    {:else}
      <li>No missed cadence surfaced.</li>
    {/each}
  </ol>
</section>
