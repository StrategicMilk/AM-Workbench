<script>
  import { MigrationRisk } from '$lib/contracts';
  import { redactSupplyChainValue, requireTrustedProvenance } from '$lib/security';
  import { migrationStore } from './store.svelte.js';

  let { projectId = 'default' } = $props();

  const riskLabels = {
    [MigrationRisk.LOW]: 'Ready',
    [MigrationRisk.CONFLICT]: 'Conflict',
    [MigrationRisk.RISKY_TOOL]: 'Tool risk',
    [MigrationRisk.SECRET]: 'Secret',
    [MigrationRisk.UNAVAILABLE]: 'Unavailable',
    [MigrationRisk.CORRUPT]: 'Corrupt',
  };

  function itemById(itemId) {
    return (migrationStore.plan?.findings ?? []).find((item) => item.item_id === itemId);
  }

  function loadProjectPlan() {
    return migrationStore.loadPlan({ project_id: projectId || 'default' });
  }

  function planEvidenceRefs() {
    return [
      ...(Array.isArray(migrationStore.plan?.evidence_refs) ? migrationStore.plan.evidence_refs : []),
      migrationStore.plan?.provenance_ref,
      migrationStore.plan?.authority_ref,
    ].filter(Boolean);
  }

  async function applySelectedImports() {
    try {
      const refs = planEvidenceRefs();
      if (refs.length === 0) {
        throw new Error('missing_migration_plan_evidence_refs');
      }
      requireTrustedProvenance({
        evidence_refs: refs,
        status: migrationStore.plan?.state ?? migrationStore.plan?.status ?? 'verified',
        reasons: migrationStore.plan?.blocked_reasons,
      }, 'migration-plan:apply');
      await migrationStore.applySelection();
    } catch (error) {
      migrationStore.lastError = error instanceof Error ? error.message : String(error);
    }
  }

  $effect(() => {
    void projectId;
    void loadProjectPlan().catch((error) => {
      migrationStore.lastError = error instanceof Error ? error.message : String(error);
    });
  });
</script>

<section class="migration-panel" data-project-id={projectId} data-testid="workbench-migration-panel">
  <header class="panel-header">
    <div>
      <h1>Migration Wizard</h1>
      <p>Dry-run import plan for prior Workbench, assistant, model, memory, skill, and tool data.</p>
    </div>
    <button type="button" aria-label="Refresh migration plan" onclick={loadProjectPlan} disabled={migrationStore.isLoading}>
      <i class="fas fa-rotate-right" aria-hidden="true"></i>
    </button>
  </header>

  {#if migrationStore.lastError}
    <div class="status-banner error" role="alert">{migrationStore.lastError}</div>
  {/if}

  <div class="summary-row" aria-label="Migration summary">
    <span>{migrationStore.plan?.findings?.length ?? 0} found</span>
    <span>{migrationStore.riskyCount} need review</span>
    <span>{migrationStore.selectedCount} selected</span>
  </div>

  {#if migrationStore.plan?.conflicts?.length}
    <section class="conflicts" aria-label="Conflicts">
      <h2>Conflicts</h2>
      {#each migrationStore.plan.conflicts as conflict}
        <div class="conflict-row">
          <span>{conflict.destination_path}</span>
          <select
            aria-label="Conflict selection"
            value={migrationStore.conflictSelections[conflict.conflict_key] ?? ''}
            onchange={(event) => migrationStore.chooseConflict(conflict, event.currentTarget.value)}
          >
            <option value="">Choose import</option>
            {#each conflict.candidate_item_ids as itemId}
              <option value={itemId}>{itemById(itemId)?.label ?? itemId}</option>
            {/each}
          </select>
        </div>
      {/each}
    </section>
  {/if}

  <div class="finding-list" role="list" aria-label="Migration findings">
    {#each migrationStore.plan?.findings ?? [] as item}
      <article class:risky={item.risk !== MigrationRisk.LOW} role="listitem">
        <label class="finding-toggle">
          <input
            type="checkbox"
            checked={migrationStore.selected.has(item.item_id)}
            onchange={() => migrationStore.toggleItem(item)}
          />
          <span>
            <strong>{item.label}</strong>
            <small>{redactSupplyChainValue(item.path, 'local_path')}</small>
          </span>
        </label>
        <span class="risk" data-risk={item.risk}>{riskLabels[item.risk] ?? item.risk}</span>
        {#if item.risk === MigrationRisk.SECRET}
          <label class="secret-opt-in">
            <input
              type="checkbox"
              checked={migrationStore.secretSelection.has(item.item_id)}
              onchange={(event) => migrationStore.setSecretIncluded(item, event.currentTarget.checked)}
            />
            Include secret
          </label>
        {/if}
        <pre>{redactSupplyChainValue(item.redacted_preview, item.risk === MigrationRisk.SECRET ? 'secret' : 'preview')}</pre>
      </article>
    {/each}
  </div>

  <footer class="panel-footer">
    <label class="backup-confirmation">
      <input type="checkbox" bind:checked={migrationStore.backupConfirmed} />
      <span>Backup completed and reviewed before applying selected imports.</span>
    </label>
    <button
      type="button"
      onclick={applySelectedImports}
      disabled={!migrationStore.plan || migrationStore.isLoading || !migrationStore.backupConfirmed}
    >
      <i class="fas fa-file-import" aria-hidden="true"></i>
      Apply
    </button>
    {#if migrationStore.result}
      <span class="apply-result">{migrationStore.result.status}: {migrationStore.result.report_path ?? migrationStore.result.blocked_reasons?.join(', ') ?? 'no receipt path returned'}</span>
    {/if}
  </footer>
</section>

<style>
  .migration-panel {
    display: grid;
    gap: 14px;
    padding: 18px;
    color: var(--text-primary, #e5e7eb);
  }

  .panel-header,
  .summary-row,
  .conflict-row,
  .panel-footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }

  h1,
  h2,
  p {
    margin: 0;
  }

  h1 {
    font-size: 1.35rem;
  }

  h2 {
    font-size: 1rem;
  }

  p,
  small,
  .apply-result {
    color: var(--text-muted, #94a3b8);
  }

  button,
  select {
    min-height: 44px;
    border: 1px solid var(--border-default, #334155);
    border-radius: 6px;
    background: var(--surface-elevated, #111827);
    color: var(--text-primary, #e5e7eb);
  }

  .summary-row,
  .conflicts {
    border: 1px solid var(--border-default, #334155);
    border-radius: 8px;
    padding: 10px 12px;
  }

  .finding-list {
    display: grid;
    gap: 10px;
  }

  article {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 8px 12px;
    border: 1px solid var(--border-default, #334155);
    border-radius: 8px;
    padding: 12px;
    background: var(--surface-primary, #0f172a);
  }

  article.risky {
    border-color: #b45309;
  }

  .finding-toggle,
  .secret-opt-in,
  .backup-confirmation {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .finding-toggle span {
    display: grid;
    min-width: 0;
  }

  .risk {
    align-self: start;
    border-radius: 999px;
    border: 1px solid var(--border-default, #334155);
    padding: 4px 8px;
    font-size: 0.78rem;
  }

  .risk[data-risk='secret'],
  .risk[data-risk='risky_tool'] {
    border-color: #dc2626;
  }

  pre {
    grid-column: 1 / -1;
    max-height: 92px;
    overflow: auto;
    margin: 0;
    white-space: pre-wrap;
    color: var(--text-muted, #94a3b8);
    font-size: 0.78rem;
  }

  .status-banner {
    border-radius: 8px;
    padding: 10px 12px;
  }

  .error {
    border: 1px solid #dc2626;
    color: #fecaca;
  }
</style>
