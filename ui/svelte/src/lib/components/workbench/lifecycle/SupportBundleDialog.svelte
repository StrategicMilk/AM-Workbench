<script>
  import { requireEvidence } from '$lib/evidence/evidenceGuard.js';

  let { onSubmit = () => {}, result = null, error = null, redactionEvidenceRef = '' } = $props();
  let destinationPath = $state('launcher-support.zip');
  let localError = $state('');

  function submitBundle() {
    localError = '';
    try {
      const [redactionRef] = requireEvidence([redactionEvidenceRef], 'support-bundle:redaction');
      if (!redactionRef) {
        throw new Error('missing_redaction_evidence_ref');
      }
      onSubmit({ destination_path: destinationPath, redaction_evidence_ref: redactionRef });
    } catch (err) {
      localError = err instanceof Error ? err.message : String(err);
    }
  }
</script>

<section class="support-bundle" data-testid="launcher-support-bundle" aria-label="Support bundle">
  <label>
    Bundle path
    <input bind:value={destinationPath} data-testid="launcher-support-destination" />
  </label>
  <button type="button" onclick={submitBundle}>Create Bundle</button>
  <p class="privacy-note" data-testid="launcher-support-redaction-disclosure">
    Redaction is applied before local-only bundle export.
  </p>
  {#if result}
    <p data-testid="launcher-support-result">{result.bundle_path}</p>
  {/if}
  {#if error || localError}
    <p class="error" data-testid="launcher-support-error">{error || localError}</p>
  {/if}
</section>

<style>
  .support-bundle {
    display: grid;
    gap: 8px;
  }

  label {
    display: grid;
    gap: 4px;
    color: var(--text-muted, #94a3b8);
    font-size: 0.85rem;
  }

  input,
  button {
    min-height: 34px;
    border: 1px solid var(--border-default, #334155);
    border-radius: 6px;
    background: var(--surface-elevated, #111827);
    color: var(--text-primary, #e5e7eb);
  }

  .error {
    color: #fca5a5;
  }

  .privacy-note {
    margin: 0;
    color: var(--text-muted, #94a3b8);
    font-size: 0.8rem;
  }
</style>
