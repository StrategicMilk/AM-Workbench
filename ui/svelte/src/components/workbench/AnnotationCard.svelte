<script lang="ts">
  import { workbenchKernelRequest } from '$lib/api.js';

  let { template, item, record_kind, annotator = '', onCommit = (_detail: object) => {} } = $props();

  let answers = $state<Record<string, any>>({});
  let localAnnotator = $state(annotator);
  let isCommitting = $state(false);
  let commitError = $state<string | null>(null);
  let isReady = $derived(Boolean(template?.fields?.every((field) => !field.required || answers[field.name] != null) && localAnnotator.trim().length > 0));

  function setAnswer(name: string, value: any) {
    answers = { ...answers, [name]: value };
  }

  function controlId(name: string) {
    return `annotation-${String(name).toLowerCase().replace(/[^a-z0-9_-]+/g, '-')}`;
  }

  async function submit() {
    if (!isReady) {
      commitError = 'missing-required-answer';
      return;
    }
    isCommitting = true;
    commitError = null;
    try {
      const json = await workbenchKernelRequest('/api/v1/workbench/annotation/commit', {
        method: 'POST',
        headers: {
          'content-type': 'application/json',
          'X-Requested-With': 'XMLHttpRequest'
        },
        body: JSON.stringify({
          template_name: template.name,
          template_version: template.version,
          record_kind,
          item,
          answers,
          source_trace_id: item?.trace_id ?? '',
          source_run_id: item?.run_id ?? '',
          annotator: localAnnotator.trim()
        })
      });
      onCommit({ asset_id: json.asset_id, lineage: json.lineage });
    } catch (error) {
      commitError = `network-error: ${error instanceof Error ? error.message : 'unknown'}`;
    } finally {
      isCommitting = false;
    }
  }
</script>

<article class="annotation-card">
  <header>
    <strong>{record_kind}</strong>
    <span>{item?.run_id ?? item?.eval_id ?? 'item'}</span>
  </header>

  {#each template?.fields ?? [] as field}
    <div class="field">
      <label for={controlId(field.name)}>{field.name}</label>
      {#if field.kind === 'text'}
        <textarea id={controlId(field.name)} aria-required={field.required ? 'true' : 'false'} oninput={(event) => setAnswer(field.name, event.currentTarget.value)}></textarea>
      {:else if field.kind === 'categorical'}
        <select id={controlId(field.name)} aria-required={field.required ? 'true' : 'false'} onchange={(event) => setAnswer(field.name, event.currentTarget.value)}>
          <option value=""></option>
          {#each field.choices ?? [] as choice}
            <option value={choice}>{choice}</option>
          {/each}
        </select>
      {:else if field.kind === 'rating'}
        <input id={controlId(field.name)} type="number" min="1" max="5" aria-required={field.required ? 'true' : 'false'} oninput={(event) => setAnswer(field.name, Number(event.currentTarget.value))} />
      {:else if field.kind === 'span'}
        <input id={controlId(field.name)} type="text" aria-required={field.required ? 'true' : 'false'} oninput={(event) => setAnswer(field.name, event.currentTarget.value)} />
      {:else if field.kind === 'boolean'}
        <input id={controlId(field.name)} type="checkbox" aria-required={field.required ? 'true' : 'false'} onchange={(event) => setAnswer(field.name, event.currentTarget.checked)} />
      {/if}
    </div>
  {/each}

  <div class="field">
    <label for="annotation-annotator">annotator</label>
    <input id="annotation-annotator" type="text" bind:value={localAnnotator} placeholder="annotator" aria-required="true" />
  </div>

  {#if commitError}
    <p class="error" role="alert">{commitError}</p>
  {/if}

  <button type="button" onclick={submit} disabled={!isReady || isCommitting}>Commit</button>
</article>

<style>
  .annotation-card {
    display: grid;
    gap: 0.75rem;
  }
  header {
    display: flex;
    gap: 0.5rem;
  }
  .field {
    display: grid;
    gap: 0.25rem;
  }
  textarea {
    min-height: 5rem;
  }
  .error {
    color: var(--color-danger, #b42318);
  }
</style>
