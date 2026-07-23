<script lang="ts">
  import { workbenchKernelRequest } from '$lib/api.js';

  interface BenchmarkProviderEntry {
    provider_id: string;
    kind: string;
    allowed_license_classifications: string[];
    allowed_privacy_classifications: string[];
    default_eval_method: string;
    description: string;
  }

  interface BenchmarkProvidersResponse {
    providers: BenchmarkProviderEntry[];
    license_classifications: string[];
    privacy_classifications: string[];
    eval_methods: string[];
  }

  interface BenchmarkImportRequest {
    provider_id: string;
    project_id: string;
    source_uri: string;
    source_kind: string;
    license_classification: string;
    privacy_classification: string;
    revision_pin: string;
    expected_output_schema: string;
    allowed_eval_method: string;
    case_payload: Record<string, string>;
  }

  interface BenchmarkImportResponse {
    eval_id: string;
    asset_id: string;
    revision_id: string;
    provider_id: string;
    source_uri: string;
    license_classification: string;
    privacy_classification: string;
    expected_output_schema: string;
    allowed_eval_method: string;
    created_at_utc: string;
  }

  let { initialProjectId = 'default' } = $props<{ initialProjectId?: string }>();
  let providers = $state<BenchmarkProviderEntry[]>([]);
  let licenseClassifications = $state<string[]>([]);
  let privacyClassifications = $state<string[]>([]);
  let evalMethods = $state<string[]>([]);
  let selectedProviderId = $state('');
  let projectId = $state('');
  let lastInitialProjectId = $state('');
  let sourceUri = $state('');
  let licenseClassification = $state('');
  let privacyClassification = $state('');
  let revisionPin = $state('');
  let expectedOutputSchema = $state('');
  let allowedEvalMethod = $state('');
  let payloadText = $state('{\n  "input": "",\n  "expected": ""\n}');
  let importResults = $state<BenchmarkImportResponse[]>([]);
  let errorMessage = $state('');
  let providersLoading = $state(false);
  let importing = $state(false);

  let selectedProvider = $derived(providers.find((provider) => provider.provider_id === selectedProviderId) ?? null);
  let formIsComplete = $derived(
    selectedProvider !== null &&
      projectId.trim().length > 0 &&
      sourceUri.trim().length > 0 &&
      licenseClassification.trim().length > 0 &&
      privacyClassification.trim().length > 0 &&
      revisionPin.trim().length > 0 &&
      expectedOutputSchema.trim().length > 0 &&
      allowedEvalMethod.trim().length > 0
  );

  $effect(() => {
    if (initialProjectId === lastInitialProjectId) return;
    if (projectId === '' || projectId === lastInitialProjectId) {
      projectId = initialProjectId;
    }
    lastInitialProjectId = initialProjectId;
  });

  function selectProvider(provider: BenchmarkProviderEntry) {
    selectedProviderId = provider.provider_id;
    licenseClassification = provider.allowed_license_classifications[0] ?? '';
    privacyClassification = provider.allowed_privacy_classifications[0] ?? '';
    allowedEvalMethod = provider.default_eval_method;
  }

  function parsePayload(): Record<string, string> {
    const parsed = JSON.parse(payloadText) as Record<string, unknown>;
    return Object.fromEntries(Object.entries(parsed).map(([key, value]) => [key, String(value)]));
  }

  function buildRequest(): BenchmarkImportRequest {
    if (!selectedProvider) {
      throw new Error('Select a provider before import');
    }
    return {
      provider_id: selectedProvider.provider_id,
      project_id: projectId.trim(),
      source_uri: sourceUri.trim(),
      source_kind: selectedProvider.kind,
      license_classification: licenseClassification,
      privacy_classification: privacyClassification,
      revision_pin: revisionPin.trim(),
      expected_output_schema: expectedOutputSchema.trim(),
      allowed_eval_method: allowedEvalMethod,
      case_payload: parsePayload(),
    };
  }

  async function loadProviders() {
    providersLoading = true;
    errorMessage = '';
    try {
      const body = (await workbenchKernelRequest('/api/workbench/benchmark/providers')) as BenchmarkProvidersResponse;
      providers = body.providers ?? [];
      licenseClassifications = body.license_classifications ?? [];
      privacyClassifications = body.privacy_classifications ?? [];
      evalMethods = body.eval_methods ?? [];
      if (!selectedProviderId && providers.length > 0) {
        selectProvider(providers[0]);
      }
    } finally {
      providersLoading = false;
    }
  }

  async function submitImport() {
    if (!formIsComplete || importing) return;
    importing = true;
    errorMessage = '';
    try {
      const requestBody = buildRequest();
      const result = (await workbenchKernelRequest('/api/workbench/benchmark/import', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
        body: JSON.stringify(requestBody),
      })) as BenchmarkImportResponse;
      importResults = [result, ...importResults];
    } catch (error) {
      errorMessage = error instanceof Error ? error.message : 'benchmark import failed';
    } finally {
      importing = false;
    }
  }

  $effect(() => {
    void loadProviders();
  });
</script>

<div class="benchmark-importer-view">
  <header class="view-header">
    <h1>Benchmark Importer</h1>
  </header>

  {#if errorMessage}
    <div class="error" role="alert">{errorMessage}</div>
  {/if}

  <section class="provider-catalog" aria-busy={providersLoading}>
    <h2>Providers</h2>
    <div class="provider-list">
      {#each providers as provider}
        <button
          type="button"
          class:selected={provider.provider_id === selectedProviderId}
          aria-pressed={provider.provider_id === selectedProviderId}
          onclick={() => selectProvider(provider)}
        >
          <span>{provider.provider_id}</span>
          <small>{provider.kind} · {provider.default_eval_method}</small>
        </button>
      {/each}
    </div>
    {#if selectedProvider}
      <dl class="provider-detail">
        <div><dt>Kind</dt><dd>{selectedProvider.kind}</dd></div>
        <div><dt>License</dt><dd>{selectedProvider.allowed_license_classifications.join(', ')}</dd></div>
        <div><dt>Privacy</dt><dd>{selectedProvider.allowed_privacy_classifications.join(', ')}</dd></div>
        <div><dt>Eval</dt><dd>{selectedProvider.default_eval_method}</dd></div>
        <div><dt>Description</dt><dd>{selectedProvider.description}</dd></div>
      </dl>
    {/if}
  </section>

  <section class="import-form">
    <h2>Import</h2>
    <form onsubmit={(event) => event.preventDefault()}>
      <label>
        Project
        <input bind:value={projectId} autocomplete="off" />
      </label>
      <label>
        Source
        <input bind:value={sourceUri} autocomplete="off" />
      </label>
      <label>
        License
        <select bind:value={licenseClassification}>
          {#each selectedProvider?.allowed_license_classifications ?? licenseClassifications as value}
            <option value={value}>{value}</option>
          {/each}
        </select>
      </label>
      <label>
        Privacy
        <select bind:value={privacyClassification}>
          {#each selectedProvider?.allowed_privacy_classifications ?? privacyClassifications as value}
            <option value={value}>{value}</option>
          {/each}
        </select>
      </label>
      <label>
        Revision
        <input bind:value={revisionPin} autocomplete="off" />
      </label>
      <label>
        Schema
        <input bind:value={expectedOutputSchema} autocomplete="off" />
      </label>
      <label>
        Method
        <select bind:value={allowedEvalMethod}>
          {#each evalMethods as value}
            <option value={value}>{value}</option>
          {/each}
        </select>
      </label>
      <label class="payload-field">
        Case payload
        <textarea bind:value={payloadText} rows="8"></textarea>
      </label>
      <div class="action-row">
        <button type="button" class="primary" disabled={!formIsComplete || importing} onclick={submitImport}>
          Import
        </button>
        <button type="button" disabled={providersLoading} onclick={loadProviders}>Refresh</button>
      </div>
    </form>
  </section>

  <section class="results">
    <h2>Results</h2>
    {#each importResults as result}
      <article>
        <h3>{result.provider_id}</h3>
        <dl>
          <div><dt>Eval</dt><dd>{result.eval_id}</dd></div>
          <div><dt>Asset</dt><dd>{result.asset_id}</dd></div>
          <div><dt>Revision</dt><dd>{result.revision_id}</dd></div>
          <div><dt>License</dt><dd>{result.license_classification}</dd></div>
          <div><dt>Privacy</dt><dd>{result.privacy_classification}</dd></div>
          <div><dt>Method</dt><dd>{result.allowed_eval_method}</dd></div>
        </dl>
      </article>
    {/each}
  </section>
</div>

<style>
  .benchmark-importer-view {
    display: grid;
    gap: 1rem;
    padding: 1rem;
    color: #17202a;
  }

  .view-header h1,
  h2,
  h3 {
    margin: 0;
    letter-spacing: 0;
  }

  section {
    border-top: 1px solid #d5dde5;
    padding-top: 1rem;
  }

  .provider-list {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(13rem, 1fr));
    gap: 0.5rem;
  }

  .provider-list button,
  .action-row button {
    min-height: 44px;
    border: 1px solid #8fa1b3;
    border-radius: 6px;
    background: #f7fafc;
    color: #17202a;
    padding: 0.65rem 0.75rem;
    text-align: left;
  }

  .provider-list button.selected,
  .action-row button.primary {
    border-color: #0f766e;
    background: #d9f2ee;
  }

  .provider-list span,
  .provider-list small {
    display: block;
    overflow-wrap: anywhere;
  }

  .provider-detail,
  article dl {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(12rem, 1fr));
    gap: 0.5rem;
  }

  form {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(14rem, 1fr));
    gap: 0.75rem;
  }

  label {
    display: grid;
    gap: 0.25rem;
    font-size: 0.9rem;
    font-weight: 600;
  }

  input,
  select,
  textarea {
    min-height: 44px;
    width: 100%;
    box-sizing: border-box;
    border: 1px solid #a9b8c6;
    border-radius: 6px;
    padding: 0.55rem;
    font: inherit;
  }

  .payload-field {
    grid-column: 1 / -1;
  }

  .action-row {
    grid-column: 1 / -1;
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
  }

  .error {
    border-left: 4px solid #b42318;
    background: #fff1f0;
    padding: 0.75rem;
  }

  article {
    border: 1px solid #d5dde5;
    border-radius: 6px;
    padding: 0.75rem;
    margin-block: 0.5rem;
  }

  dt {
    font-size: 0.75rem;
    color: #52616f;
    font-weight: 700;
  }

  dd {
    margin: 0;
    overflow-wrap: anywhere;
  }

  @media (max-width: 760px) {
    .benchmark-importer-view {
      padding: 0.75rem;
    }

    .provider-list,
    .provider-detail,
    article dl,
    form {
      grid-template-columns: 1fr;
    }

    .action-row {
      flex-direction: column;
    }

    .action-row button {
      width: 100%;
      text-align: center;
    }
  }
</style>
