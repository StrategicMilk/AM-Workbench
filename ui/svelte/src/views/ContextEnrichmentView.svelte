<script>
  import { enrichWorkbenchContext, preflightWorkbenchContextEdit } from '$lib/api.js';
  import { showToast } from '$lib/stores/toast.svelte.js';
  import HelpPopover from '$lib/components/help/HelpPopover.svelte';
  import HelpTooltip from '$lib/components/help/HelpTooltip.svelte';
  import { requireEvidence } from '$lib/evidence/evidenceGuard.js';

  let { projectId = 'default' } = $props();

  const sampleContent = 'context enrichment sample';
  const statusFixtures = ['unchanged', 'changed', 'stale', 'blocked', 'raw_read_degraded'];

  let path = $state('docs/reference/models.md');
  let content = $state(sampleContent);
  let priorDigest = $state('');
  let allowRawRead = $state(false);
  let diagnosticsAvailable = $state(true);
  let highRiskEdit = $state(false);
  let dependencyHintsText = $state('');
  let dependentHintsText = $state('');
  let loading = $state(false);
  let result = $state(null);
  let error = $state(null);

  let status = $derived(result?.status ?? 'idle');
  let digest = $derived(result?.digest ?? 'none');
  let savedBytes = $derived(result?.unchanged?.saved_body_bytes ?? 0);
  let reasons = $derived(result?.reasons ?? []);
  let contextAssets = $derived(result?.context_assets ?? []);
  let featureContext = $derived(result?.feature_context ?? null);
  let diagnostics = $derived(result?.diagnostics ?? []);
  let dependencyHints = $derived(result?.dependency_hints ?? []);

  function parseContextHintList(rawValue, fieldName) {
    const lines = rawValue
      .split(/\r?\n|,/)
      .map((item) => item.trim())
      .filter(Boolean);
    const invalidHint = lines.find((item) => !/^[a-z][a-z0-9_-]*:[^\s].*$/i.test(item));
    if (invalidHint) {
      throw new Error(`${fieldName} contains an invalid hint: ${invalidHint}`);
    }
    const evidenceGuard =
      typeof requireEvidence === 'function'
        ? requireEvidence
        : (items, label) => {
            if (!Array.isArray(items) || items.length === 0) {
              throw new Error(`${label} requires evidence`);
            }
          };
    if (lines.length > 0) {
      evidenceGuard(lines, `context_enrichment.${fieldName}`);
    }
    return lines;
  }

  function buildEnrichmentPayload(editPreflight) {
    const dependency_hints = parseContextHintList(dependencyHintsText, 'dependency_hints');
    const dependent_hints = parseContextHintList(dependentHintsText, 'dependent_hints');
    const explicitHintsProvided = dependency_hints.length > 0 && dependent_hints.length > 0;
    if ((editPreflight || highRiskEdit) && !explicitHintsProvided) {
      throw new Error('High-risk context edits require explicit dependency_hints and dependent_hints.');
    }
    return {
      path,
      content,
      prior_digest: priorDigest || null,
      project_id: projectId,
      allow_raw_read_degraded: allowRawRead,
      high_risk_edit: editPreflight || highRiskEdit,
      diagnostics_available: diagnosticsAvailable,
      provenance: { source: 'workbench-ui', project_id: projectId },
      dependency_hints,
      dependent_hints,
    };
  }

  async function submitEnrichment(editPreflight = false) {
    loading = true;
    error = null;
    try {
      const payload = buildEnrichmentPayload(editPreflight);
      result = editPreflight
        ? await preflightWorkbenchContextEdit(payload)
        : await enrichWorkbenchContext(payload);
      priorDigest = result?.digest ?? priorDigest;
      showToast('Context enrichment updated.', 'success');
    } catch (err) {
      error = err.message ?? String(err);
      showToast(`Context enrichment failed: ${error}`, 'error');
    } finally {
      loading = false;
    }
  }

  $effect(() => {
    if (!result && !loading) {
      void submitEnrichment(false);
    }
  });
</script>

<main class="context-enrichment" aria-label="Context Enrichment">
  <header class="context-header">
    <div>
      <h1>Context Enrichment</h1>
      <p>Read and enrich context assets for project {projectId}.</p>
      <HelpPopover
        title="Context enrichment"
        body="unchanged/digest elision indicator: when a context asset has not changed since the last read, the body is elided and only the digest is returned — the saved_body_bytes field shows how many bytes were elided. This reduces token usage for unchanged assets. Status values: unchanged (no change), changed (content updated), stale (digest is older than the freshness window), blocked (the path is access-controlled), raw_read_degraded (the raw read succeeded but enrichment failed). Diagnostics fallback note: if diagnostics are unavailable, the enrichment result still returns the asset content but without dependency hints or diagnostic annotations — check the diagnostics_available flag before relying on hints."
        severity="info"
      />
    </div>
    <div class="path-input">
      <input bind:value={path} aria-label="Context path" />
      <button type="button" onclick={() => submitEnrichment(false)} disabled={loading}>
        <i class="fas fa-file-lines" aria-hidden="true"></i>
        <span>Read</span>
      </button>
      <button type="button" onclick={() => submitEnrichment(true)} disabled={loading}>
        <i class="fas fa-triangle-exclamation" aria-hidden="true"></i>
        <span>Preflight</span>
      </button>
    </div>
  </header>

  <section class="controls" aria-label="Context enrichment controls">
    <label><input type="checkbox" bind:checked={allowRawRead} /> Raw degraded</label>
    <label><input type="checkbox" bind:checked={highRiskEdit} /> High risk</label>
    <label><input type="checkbox" bind:checked={diagnosticsAvailable} /> Diagnostics</label>
  </section>

  <section class="hint-controls" aria-label="Context hint controls">
    <label>
      <span>Dependency hints</span>
      <textarea
        bind:value={dependencyHintsText}
        aria-label="Dependency hints"
        placeholder="dependency:path-or-signal"
      ></textarea>
    </label>
    <label>
      <span>Dependent hints</span>
      <textarea
        bind:value={dependentHintsText}
        aria-label="Dependent hints"
        placeholder="dependent:consumer-or-task"
      ></textarea>
    </label>
  </section>

  <section class="editor" aria-label="Read content">
    <textarea bind:value={content} aria-label="Content sample"></textarea>
  </section>

  {#if loading}
    <section class="state" role="status" aria-live="polite">Loading context.</section>
  {:else if error}
    <section class="state error" role="alert">{error}</section>
  {:else}
    <section class="status-strip" aria-label="Context status">
      <span data-status={status}>{status}</span>
      <span>{digest}</span>
      <span>{savedBytes} bytes elided</span>
    </section>

    <section class="context-grid" aria-label="Context detail">
      <article>
        <h2>Status Coverage</h2>
        <ul>
          {#each statusFixtures as fixture}
            <li data-status-fixture={fixture}>{fixture}</li>
          {/each}
        </ul>
      </article>
      <article>
        <h2>Reasons</h2>
        <ul>
          {#each reasons as reason}
            <li>{reason}</li>
          {:else}
            <li>none</li>
          {/each}
        </ul>
      </article>
      <article>
        <h2>Context Assets</h2>
        <ul>
          {#each contextAssets as asset}
            <li>{asset.context_asset_id}: {asset.freshness} / {asset.usefulness_score}</li>
          {:else}
            <li>none</li>
          {/each}
        </ul>
      </article>
      <article>
        <h2>Feature Context</h2>
        <dl>
          <div><dt>view</dt><dd>{featureContext?.context_view_id ?? 'none'}</dd></div>
          <div><dt>source</dt><dd>{featureContext?.source ?? 'none'}</dd></div>
          <div><dt>version</dt><dd>{featureContext?.version ?? 'none'}</dd></div>
        </dl>
      </article>
      <article>
        <h2>Diagnostics</h2>
        <ul>
          {#each diagnostics as diagnostic}
            <li>{diagnostic}</li>
          {:else}
            <li>none</li>
          {/each}
        </ul>
      </article>
      <article>
        <h2>Dependencies</h2>
        <ul>
          {#each dependencyHints as hint}
            <li>{hint}</li>
          {:else}
            <li>none</li>
          {/each}
        </ul>
      </article>
    </section>
  {/if}
</main>

<style>
  .context-enrichment {
    display: grid;
    gap: 16px;
    min-height: 100%;
    padding: 18px;
    background: var(--bg-primary, #0b1120);
    color: var(--text-primary, #e5e7eb);
  }

  .context-header,
  .controls,
  .status-strip {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    align-items: center;
    justify-content: space-between;
  }

  .hint-controls {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 10px;
  }

  .hint-controls label {
    align-items: stretch;
    display: grid;
  }

  .hint-controls textarea {
    min-height: 68px;
  }

  h1,
  h2,
  p,
  ul,
  dl {
    margin: 0;
  }

  h1 {
    font-size: 28px;
    letter-spacing: 0;
  }

  h2 {
    font-size: 15px;
    letter-spacing: 0;
  }

  p,
  dt {
    color: var(--text-muted, #94a3b8);
  }

  .path-input {
    display: flex;
    gap: 8px;
    min-width: min(100%, 620px);
  }

  input:not([type]),
  textarea {
    min-width: 0;
    border: 1px solid var(--border-default, #334155);
    border-radius: 6px;
    background: var(--surface-elevated, #111827);
    color: inherit;
    padding: 8px 10px;
  }

  .path-input input {
    flex: 1;
  }

  textarea {
    width: 100%;
    min-height: 110px;
    resize: vertical;
  }

  button,
  label {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    min-height: 44px;
    border: 1px solid var(--border-default, #334155);
    border-radius: 6px;
    background: var(--surface-elevated, #111827);
    color: inherit;
    padding: 8px 11px;
  }

  button:disabled {
    opacity: 0.62;
    cursor: wait;
  }

  .status-strip span,
  article,
  .state,
  .editor {
    border: 1px solid var(--border-default, #334155);
    border-radius: 8px;
    background: var(--surface-elevated, #111827);
    padding: 10px;
  }

  .status-strip span {
    overflow-wrap: anywhere;
  }

  .status-strip span[data-status='unchanged'] {
    color: #86efac;
  }

  .status-strip span[data-status='changed'],
  .status-strip span[data-status='stale'] {
    color: #93c5fd;
  }

  .status-strip span[data-status='blocked'],
  .status-strip span[data-status='operator_review'],
  .status-strip span[data-status='raw_read_degraded'] {
    color: #fbbf24;
  }

  .context-grid {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 12px;
  }

  article {
    display: grid;
    gap: 10px;
  }

  li,
  dd {
    overflow-wrap: anywhere;
  }

  .error {
    color: var(--danger, #fca5a5);
  }

  @media (max-width: 900px) {
    .context-header,
    .path-input,
    .hint-controls,
    .context-grid {
      display: grid;
      grid-template-columns: 1fr;
    }
  }
</style>
