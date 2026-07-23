<script lang="ts">
  import { workbenchKernelRequest } from '$lib/api.js';

  let { template = null, onSubmit = () => {}, onCancel = () => {} } = $props();

  let draft = $state({
    name: '',
    version: '1',
    record_kind: 'answer',
    fields: [{ name: 'quality', kind: 'rating', choices: [], required: true }]
  });
  let validationError = $state<string | null>(null);
  let isValidating = $state(false);
  let validSummary = $derived(validationError === null && draft.fields.length > 0 ? 'ready' : 'pending');

  $effect(() => {
    if (template) {
      draft = JSON.parse(JSON.stringify(template));
    }
  });

  function addField() {
    draft.fields = [...draft.fields, { name: '', kind: 'text', choices: [], required: true }];
  }

  function updateChoices(index: number, value: string) {
    draft.fields[index].choices = value.split(',').map((choice) => choice.trim()).filter(Boolean);
  }

  function localValidationError() {
    if (!draft.name.trim()) return 'template-name-required';
    if (!draft.version.trim()) return 'template-version-required';
    if (!draft.fields.length) return 'template-field-required';
    if (draft.fields.some((field) => !String(field.name ?? '').trim())) return 'field-name-required';
    return null;
  }

  async function validate() {
    isValidating = true;
    validationError = localValidationError();
    if (validationError) {
      isValidating = false;
      return;
    }
    try {
      const json = await workbenchKernelRequest('/api/v1/workbench/annotation/templates', {
        method: 'POST',
        headers: {
          'content-type': 'application/json',
          'X-Requested-With': 'XMLHttpRequest'
        },
        body: JSON.stringify({ template: draft })
      });
      onSubmit(json);
    } catch (error) {
      validationError = `network-error: ${error instanceof Error ? error.message : 'unknown'}`;
    } finally {
      isValidating = false;
    }
  }
</script>

<section class="annotation-template" data-state={validSummary}>
  <header>
    <input bind:value={draft.name} placeholder="Template name" aria-label="Template name" />
    <input bind:value={draft.version} placeholder="Version" aria-label="Template version" />
    <select bind:value={draft.record_kind} aria-label="Record kind">
      <option value="trace">trace</option>
      <option value="answer">answer</option>
      <option value="tool_call">tool_call</option>
      <option value="rag_chunk">rag_chunk</option>
      <option value="multimodal">multimodal</option>
    </select>
  </header>

  <div class="fields">
    {#each draft.fields as field, index}
      <div class="field-row">
        <input bind:value={field.name} placeholder="Field name" aria-label="Field name" />
        <select bind:value={field.kind} aria-label="Field kind">
          <option value="text">text</option>
          <option value="categorical">categorical</option>
          <option value="rating">rating</option>
          <option value="span">span</option>
          <option value="boolean">boolean</option>
        </select>
        {#if field.kind === 'text'}
          <span>text</span>
        {:else if field.kind === 'categorical'}
          <input value={field.choices?.join(', ') ?? ''} oninput={(event) => updateChoices(index, event.currentTarget.value)} placeholder="Choices" />
        {:else if field.kind === 'rating'}
          <span>rating</span>
        {:else if field.kind === 'span'}
          <span>span</span>
        {:else if field.kind === 'boolean'}
          <span>boolean</span>
        {/if}
      </div>
    {/each}
  </div>

  {#if validationError}
    <p class="error" role="alert" aria-live="assertive">{validationError}</p>
  {/if}

  <footer>
    <button type="button" onclick={addField}>Add field</button>
    <button type="button" onclick={onCancel}>Cancel</button>
    <button type="button" onclick={validate} disabled={isValidating}>Submit</button>
  </footer>
</section>

<style>
  .annotation-template {
    display: grid;
    gap: 0.75rem;
  }
  header,
  .field-row,
  footer {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
  }
  input,
  select,
  button {
    min-height: 2rem;
  }
  .fields {
    display: grid;
    gap: 0.5rem;
  }
  .error {
    color: var(--color-danger, #b42318);
  }
</style>
