<script>
  import { requireEvidence } from '$lib/evidence/evidenceGuard.js';

  let { store } = $props();

  let name = $state('Daily rhythm check');
  let intervalHours = $state(24);
  let sourceContext = $state('user-authored routine');
  let consentRef = $state('consent:habit-health-local');
  let provenanceRef = $state('receipt:habit-health-local');
  let status = $state('idle');
  let errorMessage = $state('');

  const canSave = $derived(
    Boolean(name.trim() && Number(intervalHours) >= 1 && sourceContext.trim() && consentRef.trim() && provenanceRef.trim()),
  );

  function routineIdFromName(value) {
    const slug = value
      .trim()
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/^-|-$/g, '');
    return `routine-${slug || 'habit-health'}`;
  }

  async function saveRoutine() {
    if (!canSave) {
      return;
    }
    errorMessage = '';
    status = 'saving';
    try {
      const [consent, provenance] = requireEvidence([consentRef, provenanceRef], 'habit-routine:save');
      await store.createRoutine({
        name: name.trim(),
        routine_id: routineIdFromName(name),
        cadence: { interval_hours: Number(intervalHours), grace_hours: 2 },
        scope: 'personal_wellness',
        source_context: sourceContext.trim(),
        consent_refs: [consent],
        provenance_ref: provenance,
      });
      status = 'saved';
    } catch (error) {
      status = 'blocked';
      errorMessage = error instanceof Error ? error.message : 'Routine save blocked';
    }
  }
</script>

<section class="panel routine-editor" aria-label="Routine editor">
  <h2>Routine</h2>
  <label>
    <span>Name</span>
    <input bind:value={name} />
  </label>
  <label>
    <span>Cadence hours</span>
    <input type="number" min="1" bind:value={intervalHours} />
  </label>
  <label>
    <span>Source</span>
    <input bind:value={sourceContext} />
  </label>
  <label>
    <span>Consent</span>
    <input bind:value={consentRef} />
  </label>
  <label>
    <span>Provenance</span>
    <input bind:value={provenanceRef} />
  </label>
  <button type="button" disabled={!canSave || status === 'saving'} onclick={saveRoutine}>
    <i class="fas fa-plus" aria-hidden="true"></i>
    <span>Save</span>
  </button>
  <output role={status === 'blocked' ? 'alert' : 'status'} aria-live={status === 'blocked' ? 'assertive' : 'polite'}>
    {errorMessage || status}
  </output>
</section>
