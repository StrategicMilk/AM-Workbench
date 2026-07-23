<script>
  import Icon from '$lib/a11y/Icon.svelte';
  import { onMount } from 'svelte';

  let {
    disabled = false,
    onGrammarChange = () => {},
    templateStorageKey = 'vetinari:grammar-templates:default',
  } = $props();

  let mode = $state('json_schema');
  let schemaText = $state('{"type":"object","properties":{"answer":{"type":"string"}}}');
  let savedTemplates = $state([]);
  let selectedTemplate = $state('');
  let error = $state('');
  let storageError = $state('');

  function storageAvailable() {
    return typeof window !== 'undefined' && window.localStorage;
  }

  function normalizeTemplates(rawTemplates) {
    if (!Array.isArray(rawTemplates)) return [];
    return rawTemplates
      .filter((template) => template && typeof template === 'object')
      .map((template) => ({
        name: String(template.name ?? '').trim(),
        mode: template.mode === 'gbnf' ? 'gbnf' : 'json_schema',
        body: String(template.body ?? ''),
      }))
      .filter((template) => template.name && template.body.trim());
  }

  function persistTemplates(nextTemplates) {
    if (!storageAvailable()) {
      storageError = 'Template storage is unavailable.';
      return;
    }
    try {
      window.localStorage.setItem(templateStorageKey, JSON.stringify(nextTemplates));
      storageError = '';
    } catch (err) {
      storageError = err.message ?? String(err);
    }
  }

  function loadTemplates() {
    if (!storageAvailable()) return;
    try {
      const parsed = JSON.parse(window.localStorage.getItem(templateStorageKey) ?? '[]');
      savedTemplates = normalizeTemplates(parsed);
      storageError = '';
    } catch (err) {
      savedTemplates = [];
      storageError = err.message ?? String(err);
    }
  }

  function buildConfig() {
    const text = schemaText.trim();
    if (!text) return { response_format: null, grammar: null };
    if (mode === 'json_schema') {
      try {
        JSON.parse(text);
      } catch (err) {
        error = err.message ?? String(err);
        return { response_format: null, grammar: null };
      }
      error = '';
      return { response_format: 'json', grammar: text };
    }
    error = '';
    return { response_format: null, grammar: text };
  }

  function emitConfig() {
    onGrammarChange(buildConfig());
  }

  function saveTemplate() {
    if (disabled || !schemaText.trim()) return;
    const name = `${mode}-${savedTemplates.length + 1}`;
    const nextTemplates = [...savedTemplates, { name, mode, body: schemaText }];
    savedTemplates = nextTemplates;
    selectedTemplate = name;
    persistTemplates(nextTemplates);
  }

  function applyTemplate(event) {
    const name = event.currentTarget.value;
    selectedTemplate = name;
    const template = savedTemplates.find((item) => item.name === name);
    if (template) {
      mode = template.mode;
      schemaText = template.body;
      emitConfig();
    }
  }

  onMount(loadTemplates);

  $effect(() => {
    mode;
    schemaText;
    emitConfig();
  });
</script>

<section class="grammar-panel" data-testid="fsa0055-grammar-panel" aria-label="Structured output">
  <div class="grammar-toolbar">
    <select bind:value={mode} disabled={disabled} aria-label="Output constraint mode">
      <option value="json_schema">JSON schema</option>
      <option value="gbnf">GBNF grammar</option>
    </select>
    <button type="button" onclick={saveTemplate} disabled={disabled || !schemaText.trim()} title="Save template">
      <Icon name="bookmark" />
    </button>
    <select value={selectedTemplate} onchange={applyTemplate} disabled={savedTemplates.length === 0} aria-label="Saved templates">
      <option value="">Templates</option>
      {#each savedTemplates as template (template.name)}
        <option value={template.name}>{template.name}</option>
      {/each}
    </select>
  </div>

  <textarea
    bind:value={schemaText}
    disabled={disabled}
    rows="5"
    aria-label="Grammar or JSON schema"
  ></textarea>

  {#if error}
    <p class="grammar-error" role="alert">{error}</p>
  {/if}
  {#if storageError}
    <p class="grammar-error" role="alert">{storageError}</p>
  {/if}
</section>

<style>
  .grammar-panel {
    display: grid;
    gap: 8px;
    border: 1px solid var(--border-default);
    border-radius: 8px;
    background: var(--surface-elevated);
    padding: 10px;
  }

  .grammar-toolbar {
    display: grid;
    grid-template-columns: minmax(120px, 1fr) 38px minmax(120px, 1fr);
    gap: 6px;
    align-items: center;
  }

  select,
  textarea,
  button {
    border: 1px solid var(--border-default);
    border-radius: 6px;
    background: var(--surface-bg);
    color: var(--text-primary);
    font: inherit;
    min-width: 0;
  }

  select,
  button {
    min-height: 34px;
  }

  textarea {
    resize: vertical;
    padding: 8px;
    font-family: var(--font-mono);
    font-size: 0.78rem;
  }

  button {
    display: inline-flex;
    align-items: center;
    justify-content: center;
  }

  .grammar-error {
    margin: 0;
    color: var(--danger);
    font-size: 0.76rem;
  }

  @media (max-width: 640px) {
    .grammar-toolbar {
      grid-template-columns: 1fr;
    }
  }
</style>
