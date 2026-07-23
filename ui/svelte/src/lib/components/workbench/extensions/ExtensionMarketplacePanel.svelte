<script>
  import * as api from '$lib/api.js';
  import { requireEvidence } from '$lib/evidence/evidenceGuard.js';

  let extensions = $state([]);
  let selectedId = $state('');
  let selected = $state(null);
  let loading = $state(false);
  let error = $state('');
  let actionMessage = $state('');

  const selectedExtension = $derived(selected?.extension ?? selected);
  const selectedRegistration = $derived(selected?.registration ?? null);
  const selectedEvidenceIssue = $derived(evidenceIssue(selectedExtension));

  function extensionId(extension) {
    return extension?.extension_id ?? extension?.id ?? extension?.name ?? '';
  }

  async function loadExtensions() {
    loading = true;
    error = '';
    try {
      const data = await api.listWorkbenchExtensions();
      extensions = Array.isArray(data) ? data : data.extensions ?? [];
      if (!selectedId && extensions.length > 0) {
        await selectExtension(extensionId(extensions[0]));
      }
    } catch (err) {
      error = err.message;
      extensions = [];
    } finally {
      loading = false;
    }
  }

  async function selectExtension(extensionId) {
    selectedId = extensionId;
    actionMessage = '';
    try {
      selected = await api.getWorkbenchExtension(extensionId);
    } catch (err) {
      selected = null;
      error = err.message;
    }
  }

  async function requestSelection() {
    if (!selectedId) return;
    actionMessage = '';
    try {
      const data = await api.selectWorkbenchExtension(selectedId);
      actionMessage = data.decision?.enabled ? 'Enabled' : 'Blocked by backend policy';
      await selectExtension(selectedId);
    } catch (err) {
      actionMessage = err.message;
      await selectExtension(selectedId);
    }
  }

  function reasons(verdict) {
    return verdict?.reasons ?? [];
  }

  function evidenceIssue(extension) {
    if (!extension) return '';
    const refs = [
      ...(Array.isArray(extension.evidence_refs) ? extension.evidence_refs : []),
      ...(Array.isArray(extension.risk_verdict?.evidence_refs) ? extension.risk_verdict.evidence_refs : []),
      extension.marketplace_metadata?.source_id,
      extension.authority_owner,
    ].filter(Boolean);
    if (refs.length === 0) {
      return 'missing_extension_evidence';
    }
    try {
      requireEvidence(refs, `extension-marketplace:${extensionId(extension) || 'selected'}`);
      return '';
    } catch (error) {
      return error.message;
    }
  }

  $effect(() => {
    loadExtensions();
  });
</script>

<section class="extensions-panel" aria-label="Workbench extension marketplace">
  <header class="panel-header">
    <div>
      <h2>Extension Marketplace</h2>
      <p>Curated and imported MCP/plugin metadata with Workbench-owned safety verdicts.</p>
    </div>
    <button type="button" class="refresh-button" onclick={loadExtensions} disabled={loading} title="Refresh marketplace">
      <i class="fas fa-rotate"></i>
    </button>
  </header>

  {#if error}
    <div class="status-banner blocked" role="alert">{error}</div>
  {/if}

  <div class="marketplace-grid">
    <div class="extension-list" role="list" aria-label="Extensions">
      {#each extensions as extension}
        <button
          type="button"
          class="extension-row"
          class:selected={selectedId === extensionId(extension)}
          onclick={() => selectExtension(extensionId(extension))}
        >
          <span class="row-title">{extensionId(extension)}</span>
          <span class="row-meta">{extension.source_kind ?? extension.source ?? 'unknown'} / {extension.version ?? 'unpinned'}</span>
          <span class:blocked={extension.risk_verdict?.status !== 'allowed'} class="risk-chip">
            {extension.risk_verdict?.status ?? 'blocked'}
          </span>
        </button>
      {/each}
    </div>

    <article class="extension-detail">
      {#if selectedExtension}
        <div class="detail-title">
          <div>
            <h3>{extensionId(selectedExtension)}</h3>
            <p>{selectedExtension.marketplace_metadata?.source_id} / {selectedExtension.marketplace_metadata?.package_pin}</p>
          </div>
          <span class="risk-chip" class:blocked={selectedExtension.risk_verdict?.status !== 'allowed'}>
            {selectedExtension.risk_verdict?.status ?? 'blocked'}
          </span>
        </div>

        <div class="summary-grid">
          <div>
            <span class="label">Compatibility</span>
            <strong>{selectedExtension.compatibility}</strong>
          </div>
          <div>
            <span class="label">Manual selection</span>
            <strong>{selectedExtension.risk_verdict?.manual_selection_required ? 'Required' : 'Not required'}</strong>
          </div>
          <div>
            <span class="label">Disabled</span>
            <strong>{selectedExtension.disabled_by_default ? 'Yes' : 'No'}</strong>
          </div>
          <div>
            <span class="label">Registration</span>
            <strong>{selectedRegistration?.status ?? 'blocked'}</strong>
          </div>
        </div>

        <section class="detail-section">
          <h4>Risk</h4>
          <div class="token-list">
            {#each reasons(selectedExtension.risk_verdict) as reason}
              <span>{reason}</span>
            {/each}
          </div>
        </section>

        <section class="detail-section">
          <h4>Overlap and Capability</h4>
          <div class="token-list">
            {#each selectedExtension.overlap_categories ?? [] as overlap}
              <span>{overlap}</span>
            {/each}
            {#each selectedExtension.destructive_capabilities ?? [] as destructive}
              <span class="blocked">{destructive}</span>
            {/each}
          </div>
        </section>

        <section class="detail-section">
          <h4>Secrets and Dependencies</h4>
          <ul>
            {#each selectedExtension.requested_secrets ?? [] as secret}
              <li>{secret.name} / {secret.scope} / {secret.value}</li>
            {/each}
            {#each selectedExtension.dependency_findings ?? [] as finding}
              <li>{finding}</li>
            {/each}
            {#if (selectedExtension.requested_secrets ?? []).length === 0 && (selectedExtension.dependency_findings ?? []).length === 0}
              <li>No secret values exposed; no dependency findings.</li>
            {/if}
          </ul>
        </section>

        <div class="actions">
          <button type="button" onclick={requestSelection} disabled={selectedRegistration?.enabled || Boolean(selectedEvidenceIssue)}>
            <i class="fas fa-check"></i>
            Manual enablement
          </button>
          <span>{selectedEvidenceIssue || actionMessage}</span>
        </div>
      {:else}
        <p class="empty-state">No extension selected.</p>
      {/if}
    </article>
  </div>
</section>

<style>
  .extensions-panel {
    display: flex;
    flex-direction: column;
    gap: 18px;
    padding: 24px;
    color: var(--text-primary);
  }

  .panel-header,
  .detail-title,
  .actions {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
  }

  h2,
  h3,
  h4,
  p {
    margin: 0;
  }

  .panel-header p,
  .detail-title p,
  .label,
  .row-meta,
  .actions span {
    color: var(--text-muted);
    font-size: 13px;
  }

  .refresh-button,
  .actions button {
    border: 1px solid var(--border-default);
    background: var(--surface-elevated);
    color: var(--text-primary);
    border-radius: 6px;
    min-height: 36px;
    padding: 0 12px;
    cursor: pointer;
  }

  .marketplace-grid {
    display: grid;
    grid-template-columns: minmax(240px, 340px) minmax(0, 1fr);
    gap: 18px;
  }

  .extension-list,
  .extension-detail {
    border: 1px solid var(--border-default);
    background: var(--surface-elevated);
    border-radius: 8px;
  }

  .extension-list {
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  .extension-row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 4px 12px;
    padding: 12px;
    border: 0;
    border-bottom: 1px solid var(--border-default);
    background: transparent;
    color: var(--text-primary);
    text-align: left;
    cursor: pointer;
  }

  .extension-row.selected,
  .extension-row:hover {
    background: var(--glass-bg, rgba(255, 255, 255, 0.05));
  }

  .row-title {
    font-weight: 600;
  }

  .risk-chip {
    grid-row: span 2;
    align-self: center;
    border: 1px solid var(--border-default);
    border-radius: 999px;
    padding: 3px 8px;
    font-size: 12px;
    text-transform: uppercase;
  }

  .risk-chip.blocked,
  .blocked {
    color: var(--danger, #f87171);
  }

  .extension-detail {
    display: flex;
    flex-direction: column;
    gap: 18px;
    min-height: 440px;
    padding: 18px;
  }

  .summary-grid {
    display: grid;
    grid-template-columns: repeat(4, minmax(120px, 1fr));
    gap: 12px;
  }

  .summary-grid > div {
    border: 1px solid var(--border-default);
    border-radius: 6px;
    padding: 10px;
  }

  .label {
    display: block;
    margin-bottom: 4px;
  }

  .detail-section {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .token-list {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }

  .token-list span {
    border: 1px solid var(--border-default);
    border-radius: 999px;
    padding: 4px 8px;
    font-size: 12px;
  }

  ul {
    margin: 0;
    padding-left: 18px;
    color: var(--text-secondary, var(--text-primary));
  }

  .status-banner {
    border: 1px solid currentColor;
    border-radius: 6px;
    padding: 10px 12px;
  }

  .empty-state {
    color: var(--text-muted);
  }

  @media (max-width: 900px) {
    .marketplace-grid,
    .summary-grid {
      grid-template-columns: 1fr;
    }
  }
</style>
