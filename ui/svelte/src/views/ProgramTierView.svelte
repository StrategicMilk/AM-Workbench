<script>
  import { getProgramTierDetail, getProgramTierOverview } from '$lib/api.js';
  import { showToast } from '$lib/stores/toast.svelte.js';

  let payload = $state(null);
  let selected = $state(null);
  let detail = $state(null);
  let loading = $state(true);
  let detailLoading = $state(false);
  let error = $state('');

  let programs = $derived(payload?.programs ?? []);
  let selectedProgram = $derived(programs.find((program) => program.program_id === selected) ?? programs[0] ?? null);
  let packs = $derived(detail?.packs ?? selectedProgram?.packs ?? []);

  async function loadPrograms() {
    loading = true;
    error = '';
    try {
      const data = await getProgramTierOverview();
      payload = data;
      selected = data.programs?.[0]?.program_id ?? null;
    } catch (err) {
      error = err.message ?? String(err);
      showToast(`Program tier load failed: ${error}`, 'error');
    } finally {
      loading = false;
    }
  }

  async function loadDetail(programId) {
    if (!programId) return;
    detailLoading = true;
    try {
      detail = await getProgramTierDetail(programId);
    } catch (err) {
      detail = null;
      showToast(`Program detail load failed: ${err.message ?? String(err)}`, 'error');
    } finally {
      detailLoading = false;
    }
  }

  function selectProgram(programId) {
    selected = programId;
    detail = null;
    loadDetail(programId);
  }

  $effect(() => {
    loadPrograms();
  });

  $effect(() => {
    if (selected) loadDetail(selected);
  });
</script>

<section class="program-tier" aria-label="Program tier execution">
  <header>
    <div>
      <h1>Program Tier</h1>
      <p>Waves, packs, and PROGRAM.md execution state.</p>
    </div>
    <button type="button" onclick={loadPrograms} disabled={loading}>
      <i class="fas fa-sync-alt" class:fa-spin={loading} aria-hidden="true"></i>
      Refresh
    </button>
  </header>

  {#if loading}
    <div class="state" role="status">Loading program state.</div>
  {:else if error}
    <div class="state error" role="alert">{error}</div>
  {:else if programs.length === 0}
    <div class="state empty" role="status">No program records found.</div>
  {:else}
    <section class="summary" aria-label="Program summary">
      <div><span>Programs</span><strong>{payload?.summary?.program_count ?? programs.length}</strong></div>
      <div><span>Selected Wave</span><strong>{detail?.current_wave ?? selectedProgram?.current_wave ?? 'none'}</strong></div>
      <div><span>Packs Complete</span><strong>{selectedProgram?.packs_complete ?? 0}</strong></div>
      <div><span>Packs Total</span><strong>{selectedProgram?.packs_total ?? packs.length}</strong></div>
    </section>

    <div class="workspace">
      <nav class="program-list" aria-label="Program list">
        {#each programs as program}
          <button
            type="button"
            class:selected={selectedProgram?.program_id === program.program_id}
            aria-pressed={selectedProgram?.program_id === program.program_id}
            onclick={() => selectProgram(program.program_id)}
          >
            <strong>{program.program_id}</strong>
            <span>{program.phase ?? 'unknown'} - wave {program.current_wave ?? 'none'}</span>
          </button>
        {/each}
      </nav>

      <section class="program-detail" aria-busy={detailLoading} aria-label="Selected program detail">
        <header>
          <div>
            <h2>{detail?.program_id ?? selectedProgram?.program_id}</h2>
            <p>{detail?.program_path ?? selectedProgram?.program_path}</p>
          </div>
          <span>{detail?.phase ?? selectedProgram?.phase ?? 'unknown'}</span>
        </header>
        <div class="pack-table-wrap">
          <table>
            <thead>
              <tr><th>Pack</th><th>Run</th><th>Review</th><th>Wave</th></tr>
            </thead>
            <tbody>
              {#each packs as pack}
                <tr>
                  <td>{pack.slug ?? pack.pack_id ?? 'pack'}</td>
                  <td>{pack.run_status ?? 'unknown'}</td>
                  <td>{pack.review_runs_status ?? 'none'}</td>
                  <td>{pack.wave ?? ''}</td>
                </tr>
              {:else}
                <tr><td colspan="4"><span role="status">No pack rows returned.</span></td></tr>
              {/each}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  {/if}
</section>

<style>
  .program-tier {
    display: grid;
    gap: 18px;
    padding: 24px;
  }
  header,
  .summary,
  .workspace {
    display: flex;
    gap: 12px;
  }
  header {
    justify-content: space-between;
    align-items: flex-start;
  }
  h1,
  h2,
  p {
    margin: 0;
  }
  button {
    border: 1px solid var(--border-default);
    background: var(--surface-elevated);
    color: var(--text-primary);
    border-radius: 8px;
    padding: 8px 10px;
    cursor: pointer;
  }
  .summary {
    display: grid;
    grid-template-columns: repeat(4, minmax(120px, 1fr));
  }
  .summary div {
    border: 1px solid var(--border-default);
    border-radius: 8px;
    padding: 12px;
    background: var(--surface-elevated);
  }
  .summary span,
  .program-list span,
  .program-detail header span {
    color: var(--text-secondary);
    font-size: 0.85rem;
  }
  .summary strong {
    display: block;
    margin-top: 4px;
    font-size: 1.4rem;
  }
  .workspace {
    display: grid;
    grid-template-columns: minmax(220px, 320px) 1fr;
  }
  .program-list {
    display: grid;
    align-content: start;
    gap: 8px;
  }
  .program-list button {
    display: grid;
    gap: 4px;
    text-align: left;
  }
  .program-list button.selected {
    border-color: var(--primary);
  }
  .program-detail {
    display: grid;
    gap: 14px;
    min-width: 0;
  }
  .pack-table-wrap {
    overflow: auto;
    border: 1px solid var(--border-default);
    border-radius: 8px;
  }
  table {
    width: 100%;
    border-collapse: collapse;
    min-width: 680px;
  }
  th,
  td {
    padding: 10px;
    border-bottom: 1px solid var(--border-default);
    text-align: left;
  }
  .state {
    padding: 16px;
    border: 1px solid var(--border-default);
    border-radius: 8px;
  }
  .error {
    color: var(--danger);
  }
  @media (max-width: 900px) {
    .summary,
    .workspace {
      grid-template-columns: 1fr;
    }
  }
</style>
