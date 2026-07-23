<script>
  import { workbenchKernelRequest } from '$lib/api.js';
  import { requireEvidence } from '$lib/evidence/evidenceGuard.js';
  import { clampUnit } from '$lib/utils/safe.js';

  let baseModel = $state('');
  let datasetPath = $state('');
  let outputDir = $state('');
  let provenanceRef = $state('');
  let consentRef = $state('');
  let safetyRef = $state('');
  let confidence = $state(0.8);
  let status = $state('idle');
  let adapterPath = $state('');
  let receiptId = $state('');
  let errorMessage = $state('');

  const fieldIds = {
    baseModel: 'adapter-training-base-model',
    datasetPath: 'adapter-training-dataset-path',
    outputDir: 'adapter-training-output-dir',
    provenanceRef: 'adapter-training-provenance-ref',
    consentRef: 'adapter-training-consent-ref',
    safetyRef: 'adapter-training-safety-ref',
    confidence: 'adapter-training-confidence',
  };

  const canSubmit = $derived(
    Boolean(
      baseModel.trim() &&
        datasetPath.trim() &&
        outputDir.trim() &&
        provenanceRef.trim() &&
        consentRef.trim() &&
        safetyRef.trim() &&
        clampUnit(confidence, 0) >= 0.7,
    ),
  );

  async function startTraining() {
    if (!canSubmit) {
      return;
    }
    status = 'running';
    adapterPath = '';
    receiptId = '';
    errorMessage = '';
    try {
      const [provenance, consent, safety] = requireEvidence(
        [provenanceRef, consentRef, safetyRef],
        'adapter-training:start',
      );
      const boundedConfidence = clampUnit(confidence, 0);
      const body = await workbenchKernelRequest('/api/training/start', {
        method: 'POST',
        body: JSON.stringify({
          skill: baseModel.trim(),
          training_mode: 'qlora',
          base_model: baseModel.trim(),
          dataset_path: datasetPath.trim(),
          output_dir: outputDir.trim(),
          provenance_ref: provenance,
          consent_ref: consent,
          safety_ref: safety,
          confidence: boundedConfidence,
        }),
      });
      if (body?.error) {
        status = 'blocked';
        errorMessage = body.error ?? 'training blocked';
        return;
      }
      status = body.status ?? 'completed';
      receiptId = body.receipt_id ?? body.job_id ?? '';
      adapterPath = body.adapter_path ? 'adapter created' : '';
    } catch (err) {
      status = 'blocked';
      errorMessage = err instanceof Error ? err.message : 'training request failed';
    }
  }
</script>

<section class="adapter-training-panel" aria-label="Adapter training">
  <div class="field-grid">
    <label>
      <span>Base model</span>
      <input id={fieldIds.baseModel} bind:value={baseModel} autocomplete="off" />
    </label>
    <label>
      <span>Dataset path</span>
      <input id={fieldIds.datasetPath} bind:value={datasetPath} autocomplete="off" />
    </label>
    <label>
      <span>Output dir</span>
      <input id={fieldIds.outputDir} bind:value={outputDir} autocomplete="off" />
    </label>
    <label>
      <span>Provenance ref</span>
      <input id={fieldIds.provenanceRef} bind:value={provenanceRef} autocomplete="off" />
    </label>
    <label>
      <span>Consent ref</span>
      <input id={fieldIds.consentRef} bind:value={consentRef} autocomplete="off" />
    </label>
    <label>
      <span>Safety ref</span>
      <input id={fieldIds.safetyRef} bind:value={safetyRef} autocomplete="off" />
    </label>
    <label>
      <span>Confidence</span>
      <input id={fieldIds.confidence} type="number" bind:value={confidence} min="0" max="1" step="0.01" />
    </label>
  </div>

  <div class="actions">
    <button type="button" disabled={!canSubmit || status === 'running'} onclick={startTraining}>
      Start QLoRA training
    </button>
    <output aria-live="polite">{receiptId || adapterPath || errorMessage || status}</output>
  </div>
</section>

<style>
  .adapter-training-panel {
    display: grid;
    gap: 12px;
    padding: 12px 0;
  }

  .field-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 10px;
  }

  label {
    display: grid;
    gap: 4px;
    font-size: 0.86rem;
  }

  input {
    min-height: 44px;
    border: 1px solid var(--border-color, #c8ced8);
    border-radius: 6px;
    padding: 4px 8px;
    background: var(--surface-color, #fff);
    color: inherit;
  }

  .actions {
    display: flex;
    align-items: center;
    gap: 12px;
    min-height: 36px;
  }

  button {
    min-height: 44px;
    border: 1px solid var(--border-color, #c8ced8);
    border-radius: 6px;
    padding: 4px 10px;
    background: var(--button-bg, #eef2f7);
    color: inherit;
  }

  button:disabled {
    opacity: 0.55;
  }

  output {
    min-width: 0;
    overflow-wrap: anywhere;
    font-size: 0.86rem;
  }
</style>
