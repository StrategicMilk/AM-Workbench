<script>
  import { showToast } from '$lib/stores/toast.svelte.js';
  import { workbenchKernelRequest } from '$lib/api.js';
  import { requireEvidence } from '$lib/evidence/evidenceGuard.js';

  let { projectId = new URLSearchParams(window.location.search).get('project_id') ?? 'default' } = $props();

  let notebooks = $state([]);
  let selectedNotebookId = $state(null);
  let selectedNotebook = $state(null);
  let selectedCellId = $state(null);
  let loading = $state(true);
  let detailLoading = $state(false);
  let error = $state(null);

  let selectedCell = $derived(selectedNotebook?.cells?.find((cell) => cell.cell_id === selectedCellId) ?? null);
  let claimCells = $derived(selectedNotebook?.cells?.filter((cell) => cell.is_product_claim) ?? []);
  let blockedCells = $derived(claimCells.filter((cell) => cell.proof_status !== 'current'));
  let rerunnableCommands = $derived(selectedCell?.rerunnable_commands ?? []);

  async function getJson(path, params = {}) {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value !== null && value !== undefined && value !== '') query.set(key, value);
    }
    const url = query.toString() ? `${path}?${query.toString()}` : path;
    return workbenchKernelRequest(url);
  }

  function validateNotebookProofRefs(notebook, context) {
    const proofRefs = (notebook?.cells ?? []).flatMap((cell) =>
      (cell.proof_refs ?? []).map((proof) => proof.ref ?? proof.proof_id),
    );
    if (proofRefs.length > 0) {
      requireEvidence(proofRefs, context);
    }
  }

  function copyCommand(command) {
    navigator.clipboard?.writeText(command)
      .then(() => showToast('Rerunnable proof command copied.', 'success'))
      .catch((err) => showToast(`Copy failed: ${err.message ?? err}`, 'error'));
  }

  function selectCell(cellId) {
    selectedCellId = cellId;
  }

  function selectCellFromKeyboard(event, cellId) {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    event.preventDefault();
    selectCell(cellId);
  }

  async function loadNotebook(notebookId) {
    if (!notebookId) return;
    detailLoading = true;
    try {
      selectedNotebook = await getJson(`/api/workbench/evidence-notebooks/${encodeURIComponent(notebookId)}`, {
        project_id: projectId,
      });
      validateNotebookProofRefs(selectedNotebook, 'evidence_notebooks.detail_proof_refs');
      if (!selectedCellId || !selectedNotebook.cells.some((cell) => cell.cell_id === selectedCellId)) {
        selectedCellId = selectedNotebook.cells[0]?.cell_id ?? null;
      }
    } catch (err) {
      error = err.message ?? String(err);
      showToast(`Evidence notebook detail failed: ${error}`, 'error');
    } finally {
      detailLoading = false;
    }
  }

  $effect(() => {
    let cancelled = false;
    loading = true;
    error = null;
    getJson('/api/workbench/evidence-notebooks', { project_id: projectId })
      .then((rows) => {
        if (cancelled) return;
        notebooks = rows;
        notebooks.forEach((notebook) => validateNotebookProofRefs(notebook, 'evidence_notebooks.list_proof_refs'));
        selectedNotebookId = rows[0]?.notebook_id ?? null;
        selectedNotebook = rows[0] ?? null;
        selectedCellId = rows[0]?.cells?.[0]?.cell_id ?? null;
        loading = false;
      })
      .catch((err) => {
        if (cancelled) return;
        error = err.message ?? String(err);
        showToast(`Evidence notebook load failed: ${error}`, 'error');
        loading = false;
      });
    return () => { cancelled = true; };
  });

  $effect(() => {
    if (selectedNotebookId) loadNotebook(selectedNotebookId);
  });
</script>

<div class="evidence-notebooks">
  <header class="notebook-header">
    <div>
      <h2>Evidence Notebooks</h2>
      <p>{projectId}</p>
    </div>
    <div class="summary-strip" aria-label="Evidence notebook summary">
      <span>{notebooks.length} notebooks</span>
      <span>{claimCells.length} claim cells</span>
      <span>{blockedCells.length} blocked</span>
    </div>
  </header>

  {#if loading}
    <div class="state">Loading proof-backed notebook cells.</div>
  {:else if error}
    <div class="state error">{error}</div>
  {:else}
    <div class="notebook-layout">
      <aside class="notebook-list" aria-label="Evidence notebook list">
        {#each notebooks as notebook (notebook.notebook_id)}
          <button
            type="button"
            class="notebook-card"
            class:selected={selectedNotebookId === notebook.notebook_id}
            aria-pressed={selectedNotebookId === notebook.notebook_id}
            onclick={() => { selectedNotebookId = notebook.notebook_id; selectedNotebook = notebook; selectedCellId = notebook.cells[0]?.cell_id ?? null; }}
          >
            <span>{notebook.title}</span>
            <small>{notebook.cells.length} cells / updated {notebook.updated_at_utc}</small>
          </button>
        {:else}
          <div class="state">No evidence notebooks are available for this project.</div>
        {/each}
      </aside>

      <main class="cell-list" aria-label="Evidence notebook claim cells">
        {#if detailLoading}
          <div class="state">Refreshing notebook proof.</div>
        {:else if selectedNotebook}
          {#each selectedNotebook.cells as cell (cell.cell_id)}
            <div
              role="button"
              tabindex="0"
              class="cell-card"
              class:selected={selectedCellId === cell.cell_id}
              class:blocked={cell.proof_status !== 'current'}
              aria-pressed={selectedCellId === cell.cell_id}
              aria-label={`Select claim cell ${cell.title}`}
              data-testid={`evidence-notebook-cell-${cell.cell_id}`}
              onclick={() => selectCell(cell.cell_id)}
              onkeydown={(event) => selectCellFromKeyboard(event, cell.cell_id)}
            >
              <header>
                <div>
                  <span class={`proof-status ${cell.proof_status}`}>{cell.proof_status}</span>
                  <h3>{cell.title}</h3>
                </div>
                <span class="purpose">{cell.purpose}</span>
              </header>
              <p>{cell.text}</p>
              <dl class="proof-facts">
                <div>
                  <dt>proof refs</dt>
                  <dd>{cell.proof_refs.length}</dd>
                </div>
                <div>
                  <dt>rerunnable</dt>
                  <dd>{cell.rerunnable_commands.length}</dd>
                </div>
              </dl>
            </div>
          {/each}
        {/if}
      </main>

      <aside class="detail-panel" aria-label="Evidence notebook cell detail">
        {#if selectedCell}
          <header>
            <div>
              <span class={`proof-status ${selectedCell.proof_status}`}>{selectedCell.proof_status}</span>
              <h3>{selectedCell.title}</h3>
            </div>
            <span class="purpose">{selectedCell.purpose}</span>
          </header>

          <p>{selectedCell.text}</p>

          <section class="proof-section" aria-label="Proof references">
            <h4>Proof references</h4>
            {#each selectedCell.proof_refs as proof (proof.kind + proof.proof_id)}
              <div class="proof-row">
                <span class={`proof-status ${proof.status}`}>{proof.status}</span>
                <div>
                  <strong>{proof.kind}</strong>
                  <small>{proof.label} / {proof.proof_id}</small>
                  {#if proof.reproduction_command}
                    <code>{proof.reproduction_command}</code>
                  {/if}
                </div>
              </div>
            {/each}
          </section>

          {#if rerunnableCommands.length > 0}
            <section class="proof-section" aria-label="Rerunnable proof commands">
              <h4>Rerunnable proof commands</h4>
              {#each rerunnableCommands as command}
                <button type="button" class="command-button" onclick={() => copyCommand(command)}>
                  <code>{command}</code>
                </button>
              {/each}
            </section>
          {/if}
        {:else}
          <div class="state">Select a cell to inspect proof refs and rerunnable commands.</div>
        {/if}
      </aside>
    </div>
  {/if}
</div>

<style>
  .evidence-notebooks { padding: 18px; max-width: 1560px; display: flex; flex-direction: column; gap: 14px; }
  .notebook-header { display: flex; justify-content: space-between; gap: 14px; align-items: flex-start; flex-wrap: wrap; }
  h2, h3, h4, p, dl { margin: 0; }
  h2 { font-size: 1.25rem; color: var(--text-primary); }
  h3 { font-size: 1rem; color: var(--text-primary); overflow-wrap: anywhere; }
  h4 { font-size: 0.78rem; text-transform: uppercase; color: var(--text-muted); }
  p, dt, dd, small { color: var(--text-muted); }
  .summary-strip { display: flex; gap: 8px; color: var(--text-muted); font-size: 0.85rem; }
  .notebook-layout { display: grid; grid-template-columns: 260px minmax(430px, 1fr) minmax(320px, 430px); gap: 12px; align-items: start; }
  .notebook-list, .cell-list, .detail-panel { display: grid; gap: 10px; }
  .notebook-card, .cell-card, .detail-panel, .state { border: 1px solid var(--border-default); background: var(--surface-elevated); border-radius: 8px; padding: 12px; }
  .notebook-card { display: grid; gap: 6px; color: var(--text-primary); text-align: left; }
  .notebook-card.selected, .cell-card.selected { border-color: var(--accent); }
  .cell-card { display: grid; gap: 10px; cursor: pointer; }
  .cell-card.blocked { border-color: var(--danger); }
  .cell-card header, .detail-panel header { display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; }
  .proof-status { border-radius: 999px; padding: 3px 8px; font-size: 0.72rem; border: 1px solid var(--border-default); text-transform: uppercase; }
  .proof-status.current { color: var(--success); }
  .proof-status.blocked, .proof-status.missing, .proof-status.unverified { color: var(--danger); }
  .purpose { color: var(--text-muted); font-size: 0.78rem; text-transform: uppercase; }
  .proof-facts { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
  .proof-facts div { border-top: 1px solid var(--border-default); padding-top: 7px; min-width: 0; }
  dt { font-size: 0.72rem; text-transform: uppercase; }
  .proof-section { display: grid; gap: 8px; }
  .proof-row { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 8px; padding: 9px; border: 1px solid var(--border-default); border-radius: 6px; }
  .proof-row div { min-width: 0; display: grid; gap: 4px; }
  button { border: 1px solid var(--border-default); background: var(--surface-default); color: var(--text-primary); border-radius: 6px; padding: 7px 10px; }
  .command-button { text-align: left; }
  code { display: block; background: var(--surface-default); color: var(--text-primary); overflow-wrap: anywhere; white-space: pre-wrap; }
  .state { padding: 28px; color: var(--text-muted); }
  .error { color: var(--danger); }
  @media (max-width: 1180px) { .notebook-layout { grid-template-columns: 1fr; } }
  @media (max-width: 700px) { .proof-facts { grid-template-columns: 1fr; } }
</style>
