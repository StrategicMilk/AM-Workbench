<script>
  /**
   * Settings and preferences view — theme, system prompts, credentials,
   * model rules, and accessibility toggles.
   */
  import { appState } from '$lib/stores/app.svelte.js';
  import { uiPreferences, HELP_DENSITY_VALUES } from '$lib/stores/uiPreferences.svelte.js';
  import Icon from '$lib/a11y/Icon.svelte';
  import HelpTooltip from '$lib/components/help/HelpTooltip.svelte';
  import HelpPopover from '$lib/components/help/HelpPopover.svelte';
  import Term from '$lib/components/help/Term.svelte';
  import * as api from '$lib/api.js';
  import { showToast } from '$lib/stores/toast.svelte.js';
  import {
    TASK_INFERENCE_PARAMETER_FALLBACKS,
    TASK_INFERENCE_PARAMETER_FIELDS,
    normalizeTaskInferenceParameters,
  } from '$lib/taskInferenceParameters.js';

  // -- State -------------------------------------------------------------------

  let credentials = $state([]);
  let rules = $state([]);
  let loading = $state(true);
  let actionPending = $state(false);
  let activeTab = $state('appearance');

  let systemPrompt = $state('');
  let systemPromptSaved = $state(false);
  let parameterPreferences = $state({ ...TASK_INFERENCE_PARAMETER_FALLBACKS });
  let parameterProfileName = $state('general');
  let parameterSaving = $state(false);
  let parameterSaved = $state(false);
  let parameterError = $state('');

  let newCredSource = $state('');
  let newCredKey = $state('');
  let newCredValue = $state('');
  let showAddCred = $state(false);
  let showCredValues = $state({});

  /** Format a timestamp as relative time string. */
  function fmt_rel(value) {
    if (!value) return '—';
    const diff = Date.now() - new Date(value).getTime();
    if (diff < 60_000) return 'just now';
    if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} min ago`;
    if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} h ago`;
    return `${Math.floor(diff / 86_400_000)} d ago`;
  }

  // -- Derived -----------------------------------------------------------------

  let isDark = $derived(appState.theme === 'dark');

  function coerceParameterValue(field, rawValue) {
    if (field.type === 'checkbox') {
      return rawValue === true;
    }

    const numericValue = Number(rawValue);
    if (!Number.isFinite(numericValue)) {
      return field.defaultValue;
    }

    const boundedValue = Math.max(field.min, Math.min(field.max, numericValue));
    return field.step >= 1 ? Math.round(boundedValue) : Number(boundedValue.toFixed(3));
  }

  function normalizeParameterPreferences(rawPreferences = {}) {
    return normalizeTaskInferenceParameters(rawPreferences);
  }

  function setParameterValue(field, rawValue) {
    parameterPreferences = {
      ...parameterPreferences,
      [field.key]: coerceParameterValue(field, rawValue),
    };
    parameterSaved = false;
  }

  async function saveParameterTuning() {
    parameterSaving = true;
    parameterError = '';
    try {
      const payload = {
        taskInferenceProfile: parameterProfileName || 'general',
        taskInferenceParameters: parameterPreferences,
      };
      const response = await api.setPreferences(payload);
      parameterPreferences = normalizeParameterPreferences(response.preferences?.taskInferenceParameters);
      parameterProfileName = response.preferences?.taskInferenceProfile ?? parameterProfileName;
      parameterSaved = true;
      showToast('Task inference parameters saved', 'success');
      setTimeout(() => { parameterSaved = false; }, 2500);
    } catch (err) {
      parameterError = err.message ?? String(err);
      showToast(`Failed to save task parameters: ${parameterError}`, 'error');
    } finally {
      parameterSaving = false;
    }
  }

  // -- Global prompt API helpers ----------------------------------------------

  /**
   * GET /api/v1/rules/global-prompt — returns { prompt: string }.
   * Used to hydrate the system prompt textarea on load.
   */
  function fetchGlobalPrompt() {
    return api.getGlobalPrompt();
  }

  // -- Data loading ------------------------------------------------------------

  async function loadData() {
    loading = true;
    try {
      // Load credentials, model-routing rules (for display), and the global
      // system prompt (so the textarea is pre-filled with the current server value).
      const [credsData, rulesData, promptData, preferencesData] = await Promise.all([
        api.listCredentials().catch(() => ({ credentials: [] })),
        // /api/v1/rules returns the full RulesManager config dict; .rules holds the list.
        api.getRules().catch(() => ({ rules: [] })),
        // /api/v1/rules/global-prompt returns { prompt: string }.
        fetchGlobalPrompt().catch(() => ({ prompt: '' })),
        api.getPreferences().catch(() => ({ preferences: {} })),
      ]);
      credentials = credsData?.credentials ?? (Array.isArray(credsData) ? credsData : []);
      rules = rulesData?.rules ?? (Array.isArray(rulesData) ? rulesData : []);
      // Pre-fill textarea with the server's current global system prompt.
      systemPrompt = promptData?.prompt ?? '';
      parameterPreferences = normalizeParameterPreferences(
        preferencesData?.preferences?.taskInferenceParameters
      );
      parameterProfileName = preferencesData?.preferences?.taskInferenceProfile ?? 'general';
    } catch (err) {
      showToast(`Failed to load settings: ${err.message}`, 'error');
    } finally {
      loading = false;
    }
  }

  $effect(() => { loadData(); });

  // -- Theme -------------------------------------------------------------------

  function toggleTheme() {
    appState.theme = isDark ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', appState.theme);
    showToast(`Theme set to ${appState.theme}`, 'info');
  }

  function setHelpDensity(value) {
    uiPreferences.helpDensity = value;
    showToast(`Help density set to ${value}`, 'info');
  }

  // -- System prompt -----------------------------------------------------------

  async function saveSystemPrompt() {
    actionPending = true;
    try {
      // POST to the dedicated global-prompt endpoint so the value reaches
      // RulesManager and applies to all agents at runtime.
      await api.saveGlobalPrompt(systemPrompt);
      systemPromptSaved = true;
      showToast('System prompt saved to server', 'success');
      setTimeout(() => { systemPromptSaved = false; }, 2500);
    } catch (err) {
      showToast(`Failed to save prompt: ${err.message}`, 'error');
    } finally {
      actionPending = false;
    }
  }

  // -- Credentials -------------------------------------------------------------

  function toggleShowCred(source) {
    showCredValues = { ...showCredValues, [source]: !showCredValues[source] };
  }

  async function handleAddCredential(e) {
    e.preventDefault();
    if (!newCredSource.trim() || !newCredKey.trim()) {
      showToast('Source type and key are required', 'warning');
      return;
    }
    actionPending = true;
    try {
      await api.setCredentials(newCredSource, { [newCredKey]: newCredValue });
      showToast(`Credentials saved for ${newCredSource}`, 'success');
      showAddCred = false;
      newCredSource = '';
      newCredKey = '';
      newCredValue = '';
      await loadData();
    } catch (err) {
      showToast(`Failed to save credentials: ${err.message}`, 'error');
    } finally {
      actionPending = false;
    }
  }

  async function handleDeleteCred(sourceType) {
    if (!confirm(`Delete credentials for ${sourceType}?`)) return;
    try {
      await api.deleteCredentials(sourceType);
      credentials = credentials.filter((c) => (c.source_type ?? c.source) !== sourceType);
      showToast(`Credentials for ${sourceType} deleted`, 'info');
    } catch (err) {
      showToast(`Failed to delete: ${err.message}`, 'error');
    }
  }

  async function handleRotateCred(sourceType) {
    actionPending = true;
    try {
      await api.rotateCredentials(sourceType);
      showToast(`Credentials rotated for ${sourceType}`, 'success');
      await loadData();
    } catch (err) {
      showToast(`Rotation failed: ${err.message}`, 'error');
    } finally {
      actionPending = false;
    }
  }
</script>

<div class="settings-view">
  <div class="view-header">
    <h2>
      <Icon name="cog" />
      Settings
    </h2>
  </div>

  <!-- Tab bar -->
  <div class="tab-bar" role="tablist" aria-label="Settings sections">
    {#each [
      { id: 'appearance', icon: 'palette', label: 'Appearance' },
      { id: 'prompts', icon: 'comment-alt', label: 'System Prompts' },
      { id: 'inference', icon: 'sliders-h', label: 'Inference' },
      { id: 'credentials', icon: 'key', label: 'Credentials' },
      { id: 'rules', icon: 'ruler', label: 'Model Rules' },
    ] as tab (tab.id)}
      <button
        class="tab"
        class:active={activeTab === tab.id}
        onclick={() => { activeTab = tab.id; }}
        role="tab"
        aria-selected={activeTab === tab.id}
        aria-controls="panel-{tab.id}"
        id="tab-{tab.id}"
      >
        <Icon name={tab.icon} />
        {tab.label}
      </button>
    {/each}
  </div>

  {#if loading}
    <div class="loading-state" role="status" aria-live="polite">
      <Icon name="spinner" class="fa-spin" />
      Loading settings...
    </div>
  {:else}
    <!-- Appearance panel -->
    {#if activeTab === 'appearance'}
      <div id="panel-appearance" role="tabpanel" aria-labelledby="tab-appearance" class="settings-panel">
        <section class="settings-section" aria-label="Theme settings">
          <h3>Theme</h3>
          <div class="setting-row">
            <div class="setting-info">
              <span class="setting-label">Color theme</span>
              <span class="setting-desc">Switch between dark and light interface.</span>
            </div>
            <button
              class="theme-toggle"
              onclick={toggleTheme}
              aria-pressed={isDark}
              aria-label="Toggle theme: currently {appState.theme}"
            >
              <Icon name={isDark ? 'moon' : 'sun'} />
              {isDark ? 'Dark' : 'Light'}
            </button>
          </div>
        </section>

        <section class="settings-section help-settings" aria-label="Help and glossary settings">
          <h3>
            Help density
            <HelpTooltip text="Controls whether non-critical inline help appears across the UI." />
          </h3>
          <div class="setting-row">
            <div class="setting-info">
              <span class="setting-label">
                Contextual help
                <Term term="CapabilityMaturity" fallback="Capability maturity" />
              </span>
              <span class="setting-desc">
                Standard keeps help available, compact hides non-critical help, and verbose opens richer help by default.
              </span>
            </div>
            <select
              class="input density-select"
              value={uiPreferences.helpDensity}
              aria-label="Help density"
              onchange={(event) => setHelpDensity(event.currentTarget.value)}
            >
              {#each HELP_DENSITY_VALUES as densityValue}
                <option value={densityValue}>{densityValue}</option>
              {/each}
            </select>
          </div>
          <div class="help-demo-row" aria-label="Help component examples">
            <HelpPopover
              title="Help density modes"
              body="Critical warnings always remain inline. Compact mode hides non-critical tips; verbose mode expands popovers so dense workflows can show more context."
            />
            <HelpTooltip
              severity="critical"
              text="Critical help remains visible even when compact density is selected."
            />
            <HelpPopover
              severity="critical"
              title="Critical warning"
              body="This warning is rendered inline and does not require hover, focus, or click."
            />
          </div>
        </section>

      </div>
    {/if}

    <!-- System prompts panel -->
    {#if activeTab === 'prompts'}
      <div id="panel-prompts" role="tabpanel" aria-labelledby="tab-prompts" class="settings-panel">
        <section class="settings-section" aria-label="System prompt configuration">
          <h3>System Prompt</h3>
          <p class="section-desc">
            Define instructions sent to the model at the start of every session.
          </p>
          <label class="form-group" aria-label="System prompt text">
            <span class="form-label">Prompt</span>
            <textarea
              class="input textarea"
              bind:value={systemPrompt}
              rows="12"
              placeholder="You are a helpful assistant..."
              aria-label="System prompt content"
            ></textarea>
          </label>
          <div class="form-actions">
            <button
              class="btn btn-primary"
              onclick={saveSystemPrompt}
              disabled={actionPending || !systemPrompt.trim()}
              aria-label="Save system prompt"
            >
              {#if systemPromptSaved}
                <Icon name="check" /> Saved
              {:else}
                <Icon name="save" /> Save Prompt
              {/if}
            </button>
          </div>
        </section>
      </div>
    {/if}

    <!-- Inference tuning panel -->
    {#if activeTab === 'inference'}
      <div id="panel-inference" role="tabpanel" aria-labelledby="tab-inference" class="settings-panel">
        <section class="settings-section parameter-tuning-panel" data-testid="fsa0051-parameter-tuning-panel" aria-label="Task inference parameter tuning">
          <h3>Task Inference Parameters</h3>
          <p class="section-desc">
            Adjust per-task generation defaults persisted through user preferences.
          </p>

          <label class="form-group">
            <span class="form-label">Profile</span>
            <input
              class="input"
              value={parameterProfileName}
              aria-label="Task inference profile"
              oninput={(event) => { parameterProfileName = event.currentTarget.value; parameterSaved = false; }}
            />
          </label>

          <div class="parameter-grid">
            {#each TASK_INFERENCE_PARAMETER_FIELDS as field (field.key)}
              <label class="parameter-control" data-parameter-key={field.key}>
                <span class="form-label">{field.label}</span>
                {#if field.type === 'checkbox'}
                  <input
                    type="checkbox"
                    checked={parameterPreferences[field.key] === true}
                    aria-label={field.key}
                    onchange={(event) => setParameterValue(field, event.currentTarget.checked)}
                  />
                {:else}
                  <input
                    class="input"
                    type="number"
                    min={field.min}
                    max={field.max}
                    step={field.step}
                    value={parameterPreferences[field.key] ?? ''}
                    placeholder={`${field.min} - ${field.max}`}
                    aria-label={field.key}
                    oninput={(event) => setParameterValue(field, event.currentTarget.value)}
                  />
                {/if}
              </label>
            {/each}
          </div>

          {#if parameterError}
            <div class="settings-error" role="alert">{parameterError}</div>
          {/if}

          <div class="form-actions">
            <button class="btn btn-primary" onclick={saveParameterTuning} disabled={parameterSaving}>
              {#if parameterSaved}
                <Icon name="check" /> Saved
              {:else}
                <Icon name="save" /> Save Parameters
              {/if}
            </button>
          </div>
        </section>
      </div>
    {/if}

    <!-- Credentials panel -->
    {#if activeTab === 'credentials'}
      <div id="panel-credentials" role="tabpanel" aria-labelledby="tab-credentials" class="settings-panel">
        <section class="settings-section" aria-label="Credentials management">
          <div class="section-header">
            <h3>Credentials</h3>
            <button
              class="btn btn-primary btn-sm"
              onclick={() => { showAddCred = !showAddCred; }}
              aria-expanded={showAddCred}
              aria-label="Add new credentials"
            >
              <Icon name="plus" /> Add
            </button>
          </div>
          <p class="section-desc">
            API keys and credentials for external services. Values are stored encrypted.
          </p>

          {#if showAddCred}
            <form class="cred-form card" onsubmit={handleAddCredential} aria-label="Add credentials form">
              <div class="form-row">
                <label class="form-group">
                  <span class="form-label">Source Type</span>
                  <input
                    type="text"
                    class="input"
                    bind:value={newCredSource}
                    placeholder="e.g. openai, anthropic, github"
                    required
                    aria-required="true"
                    aria-label="Credential source type"
                  />
                </label>
                <label class="form-group">
                  <span class="form-label">Key Name</span>
                  <input
                    type="text"
                    class="input"
                    bind:value={newCredKey}
                    placeholder="e.g. api_key"
                    required
                    aria-required="true"
                    aria-label="Credential key name"
                  />
                </label>
                <label class="form-group" style="flex: 1">
                  <span class="form-label">Value</span>
                  <input
                    type="password"
                    class="input"
                    bind:value={newCredValue}
                    placeholder="Secret value..."
                    aria-label="Credential value"
                  />
                </label>
              </div>
              <div class="form-actions">
                <button type="submit" class="btn btn-primary" disabled={actionPending}>
                  <Icon name="save" /> Save
                </button>
                <button type="button" class="btn btn-secondary" onclick={() => { showAddCred = false; }}>
                  Cancel
                </button>
              </div>
            </form>
          {/if}

          {#if credentials.length === 0}
            <div class="empty-state">
              <Icon name="key" />
              <p>No credentials configured.</p>
            </div>
          {:else}
            <ul class="cred-list" aria-label="Configured credentials">
              {#each credentials as cred ((cred.source_type ?? cred.source))}
                {@const source = cred.source_type ?? cred.source}
                <li class="cred-item">
                  <div class="cred-header">
                    <span class="cred-source">
                      <Icon name="shield-alt" />
                      {source}
                    </span>
                    <div class="cred-meta">
                      {#if cred.updated_at}
                        <span class="cred-date">Updated {fmt_rel(cred.updated_at)}</span>
                      {/if}
                      <span class="status-badge status-{cred.valid ? 'success' : 'danger'}">
                        {cred.valid ? 'Valid' : 'Invalid'}
                      </span>
                    </div>
                  </div>
                  <div class="cred-keys">
                    {#each (cred.keys ?? [cred.key ?? 'api_key']) as key}
                      <span class="cred-key">{key}</span>
                    {/each}
                  </div>
                  <div class="cred-actions">
                    <button
                      class="btn btn-secondary btn-sm"
                      onclick={() => handleRotateCred(source)}
                      disabled={actionPending}
                      aria-label="Rotate credentials for {source}"
                    >
                      <Icon name="sync-alt" /> Rotate
                    </button>
                    <button
                      class="btn btn-danger btn-sm"
                      onclick={() => handleDeleteCred(source)}
                      aria-label="Delete credentials for {source}"
                    >
                      <Icon name="trash-alt" /> Delete
                    </button>
                  </div>
                </li>
              {/each}
            </ul>
          {/if}
        </section>
      </div>
    {/if}

    <!-- Rules panel -->
    {#if activeTab === 'rules'}
      <div id="panel-rules" role="tabpanel" aria-labelledby="tab-rules" class="settings-panel">
        <section class="settings-section" aria-label="Model configuration rules">
          <h3>Model Rules</h3>
          <p class="section-desc">
            Routing and behavior rules that govern model selection and inference.
          </p>

          {#if rules.length === 0}
            <div class="empty-state">
              <Icon name="ruler" />
              <p>No rules configured.</p>
            </div>
          {:else}
            <ul class="rules-list" aria-label="Model rules">
              {#each rules as rule ((rule.id ?? rule.rule_id))}
                <li class="rule-item">
                  <div class="rule-header">
                    <span class="rule-type status-badge status-primary">{rule.type ?? 'rule'}</span>
                    <span class="rule-name">{rule.name ?? rule.id ?? 'unnamed'}</span>
                    <span class="status-badge status-{rule.enabled !== false ? 'success' : 'muted'}">
                      {rule.enabled !== false ? 'Active' : 'Disabled'}
                    </span>
                  </div>
                  {#if rule.description}
                    <p class="rule-desc">{rule.description}</p>
                  {/if}
                  {#if rule.conditions}
                    <details class="rule-conditions">
                      <summary>Conditions</summary>
                      <pre class="rule-pre">{typeof rule.conditions === 'string' ? rule.conditions : JSON.stringify(rule.conditions, null, 2)}</pre>
                    </details>
                  {/if}
                </li>
              {/each}
            </ul>
          {/if}
        </section>
      </div>
    {/if}
  {/if}
</div>


<style>
  .settings-view {
    padding: 24px;
    max-width: 900px;
  }

  .view-header {
    display: flex;
    align-items: center;
    margin-bottom: 20px;
  }

  .view-header h2 {
    font-size: 1.25rem;
    font-weight: 600;
    color: var(--text-primary);
    margin: 0;
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .view-header h2 i { color: var(--text-muted); }

  /* Tab bar */
  .tab-bar {
    display: flex;
    gap: 4px;
    border-bottom: 1px solid var(--border-default);
    margin-bottom: 24px;
    flex-wrap: wrap;
  }

  .tab {
    padding: 10px 16px;
    background: none;
    border: none;
    border-bottom: 2px solid transparent;
    color: var(--text-muted);
    font-size: 0.875rem;
    font-weight: 500;
    cursor: pointer;
    font-family: inherit;
    display: flex;
    align-items: center;
    gap: 6px;
    min-height: 44px;
    transition: color var(--transition-base), border-color var(--transition-base);
    margin-bottom: -1px;
  }

  .tab.active { color: var(--primary); border-bottom-color: var(--primary); }
  .tab:hover:not(.active) { color: var(--text-primary); }

  .settings-panel {
    display: flex;
    flex-direction: column;
    gap: 24px;
  }

  /* Sections */
  .settings-section {
    background: var(--surface-elevated);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-lg);
    padding: 20px;
  }

  .settings-section h3 {
    font-size: 1rem;
    font-weight: 600;
    color: var(--text-primary);
    margin: 0 0 4px 0;
  }

  .section-desc {
    font-size: 0.875rem;
    color: var(--text-muted);
    margin: 0 0 16px 0;
    line-height: var(--leading-relaxed);
  }

  .section-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 4px;
  }

  /* Setting rows */
  .setting-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 12px 0;
    border-top: 1px solid var(--border-subtle);
    gap: 16px;
  }

  .setting-row:first-of-type { border-top: none; }

  .setting-info { flex: 1; }

  .density-select {
    width: 160px;
    flex-shrink: 0;
    text-transform: capitalize;
  }

  .help-demo-row {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 10px;
    padding-top: 12px;
    border-top: 1px solid var(--border-subtle);
  }

  .setting-label {
    display: block;
    font-size: 0.9375rem;
    font-weight: 500;
    color: var(--text-primary);
    margin-bottom: 2px;
  }

  .setting-desc {
    font-size: 0.8125rem;
    color: var(--text-muted);
    line-height: var(--leading-relaxed);
  }

  .parameter-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 12px;
    margin-top: 16px;
  }

  .parameter-control {
    display: grid;
    gap: 6px;
    min-width: 0;
  }

  .parameter-control input[type='checkbox'] {
    width: 20px;
    height: 20px;
  }

  .settings-error {
    margin-top: 12px;
    color: var(--danger);
    font-size: 0.875rem;
  }

  /* Theme toggle button */
  .theme-toggle {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    padding: 8px 16px;
    background: var(--surface-hover);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-full);
    color: var(--text-primary);
    font-size: 0.875rem;
    font-weight: 500;
    font-family: inherit;
    cursor: pointer;
    min-height: 44px;
    transition: background var(--transition-base);
    flex-shrink: 0;
  }

  .theme-toggle:hover { background: var(--surface-pressed); }

  /* Forms */
  .form-group { display: flex; flex-direction: column; gap: 4px; }
  .form-label { font-size: 0.75rem; font-weight: 500; color: var(--text-secondary); }

  .input {
    background: var(--surface-bg);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    color: var(--text-primary);
    font-family: inherit;
    font-size: 0.875rem;
    padding: 6px 10px;
    width: 100%;
    box-sizing: border-box;
  }

  .input:focus {
    outline: 2px solid transparent;
    outline-offset: 2px;
    border-color: var(--primary);
    box-shadow: 0 0 0 2px var(--primary-muted);
  }

  .input:focus-visible {
    outline-color: var(--primary);
  }

  .textarea { resize: vertical; }

  .form-row { display: flex; gap: 12px; flex-wrap: wrap; }
  .form-actions { display: flex; gap: 8px; margin-top: 12px; }

  .card {
    background: var(--surface-bg);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-md);
    padding: 14px;
    margin-bottom: 14px;
  }

  /* Credentials */
  .cred-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 10px; }

  .cred-item {
    background: var(--surface-bg);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-md);
    padding: 12px 14px;
  }

  .cred-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 6px;
    flex-wrap: wrap;
    gap: 8px;
  }

  .cred-source {
    font-weight: 600;
    font-size: 0.9375rem;
    color: var(--text-primary);
    display: flex;
    align-items: center;
    gap: 6px;
  }

  .cred-source i { color: var(--warning); }

  .cred-meta { display: flex; align-items: center; gap: 8px; }
  .cred-date { font-size: 0.75rem; color: var(--text-muted); }

  .cred-keys {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
    margin-bottom: 10px;
  }

  .cred-key {
    background: var(--surface-hover);
    border-radius: var(--radius-sm);
    padding: 2px 8px;
    font-size: 0.75rem;
    font-family: var(--font-mono);
    color: var(--text-muted);
  }

  .cred-actions { display: flex; gap: 8px; }

  /* Rules */
  .rules-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 10px; }

  .rule-item {
    background: var(--surface-bg);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-md);
    padding: 12px 14px;
  }

  .rule-header {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
    margin-bottom: 6px;
  }

  .rule-name { font-weight: 500; font-size: 0.9375rem; color: var(--text-primary); flex: 1; }

  .rule-desc { font-size: 0.8125rem; color: var(--text-muted); margin: 0 0 8px 0; }

  .rule-conditions summary { font-size: 0.8125rem; color: var(--text-muted); cursor: pointer; margin-bottom: 6px; }

  .rule-pre {
    font-family: var(--font-mono);
    font-size: 0.75rem;
    background: var(--surface-hover);
    border-radius: var(--radius-sm);
    padding: 8px 10px;
    overflow-x: auto;
    color: var(--text-secondary);
    white-space: pre-wrap;
    word-break: break-all;
    margin: 0;
  }

  /* Status badges */
  .status-badge {
    display: inline-flex;
    align-items: center;
    padding: 2px 8px;
    border-radius: var(--radius-full);
    font-size: 0.6875rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    flex-shrink: 0;
  }

  .status-primary { background: var(--primary-muted); color: var(--primary); }
  .status-success { background: var(--success-muted); color: var(--success); }
  .status-danger  { background: var(--danger-muted);  color: var(--danger);  }
  .status-muted   { background: var(--surface-hover); color: var(--text-muted); }

  /* Buttons */
  .btn {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 8px 14px;
    border-radius: var(--radius-md);
    font-size: 0.875rem;
    font-weight: 500;
    font-family: inherit;
    cursor: pointer;
    border: none;
    min-height: 44px;
    transition: background var(--transition-base);
  }

  .btn:disabled { opacity: 0.5; cursor: not-allowed; }
  .btn-sm { padding: 6px 10px; font-size: 0.8125rem; }

  .btn-primary { background: var(--primary); color: var(--text-on-primary); }
  .btn-primary:hover:not(:disabled) { background: var(--primary-hover); }

  .btn-secondary { background: var(--surface-hover); color: var(--text-primary); border: 1px solid var(--border-default); }
  .btn-secondary:hover:not(:disabled) { background: var(--surface-pressed); }

  .btn-danger { background: var(--danger-muted); color: var(--danger); border: 1px solid rgba(240,98,98,0.3); }
  .btn-danger:hover:not(:disabled) { background: rgba(240,98,98,0.2); }

  .loading-state {
    display: flex;
    align-items: center;
    gap: 10px;
    color: var(--text-muted);
    padding: 48px 24px;
  }

  .empty-state {
    text-align: center;
    padding: 32px;
    color: var(--text-muted);
    font-size: 0.9375rem;
  }

  .empty-state i {
    font-size: 1.75rem;
    margin-bottom: 8px;
    display: block;
    opacity: 0.4;
  }

  @media (max-width: 720px) {
    .settings-view {
      max-width: none;
      padding: 16px;
    }

    .settings-section {
      padding: 16px;
    }

    .tab {
      flex: 1 1 11rem;
      justify-content: center;
    }

    .setting-row,
    .section-header,
    .form-actions,
    .cred-actions {
      align-items: stretch;
      flex-direction: column;
    }

    .density-select,
    .theme-toggle,
    .form-actions .btn,
    .cred-actions .btn {
      justify-content: center;
      width: 100%;
    }

    .cred-meta {
      flex-wrap: wrap;
    }
  }
</style>
