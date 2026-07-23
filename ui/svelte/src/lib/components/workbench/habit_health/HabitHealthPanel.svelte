<script>
  import HabitPrivacyReview from './HabitPrivacyReview.svelte';
  import HabitRhythmTimeline from './HabitRhythmTimeline.svelte';
  import HabitRoutineEditor from './HabitRoutineEditor.svelte';

  let { store } = $props();

  let routineId = $state('routine-daily-rhythm-check');
  let energy = $state(3);
  let focus = $state(3);
  let sourceContext = $state('user check-in');
  let consentRef = $state('consent:habit-health-local');

  async function checkIn() {
    await store.recordCheckIn({
      routine_id: routineId,
      energy: Number(energy),
      focus: Number(focus),
      scope: 'personal_wellness',
      source_context: sourceContext,
      consent_refs: [consentRef],
      provenance_ref: 'ui:habit-health',
      allowed_downstream_uses: ['store', 'review', 'export'],
    });
  }
</script>

<section class="habit-health-panel" aria-label="Habit health tracker panel">
  <HabitRoutineEditor {store} />

  <section class="panel check-in" aria-label="Habit check-in controls">
    <h2>Check-in</h2>
    <label>
      <span>Routine id</span>
      <input bind:value={routineId} />
    </label>
    <label>
      <span>Energy</span>
      <input type="range" min="1" max="5" bind:value={energy} />
    </label>
    <label>
      <span>Focus</span>
      <input type="range" min="1" max="5" bind:value={focus} />
    </label>
    <label>
      <span>Source</span>
      <input bind:value={sourceContext} />
    </label>
    <button type="button" onclick={checkIn}>
      <i class="fas fa-check"></i>
      <span>Record</span>
    </button>
  </section>

  <HabitRhythmTimeline summary={store.summary} />
  <HabitPrivacyReview {store} />
</section>
