<script>
  let { preview = null, validation = null } = $props();
  let steps = $derived(preview?.ordered_steps ?? []);
</script>

<section class="preview" aria-label="Workflow preview">
  <header>
    <h2>Preview</h2>
    <span class:pass={validation?.passed} role="status" aria-live="polite">
      {validation?.passed ? 'valid' : 'blocked'}
    </span>
  </header>
  {#if validation?.errors?.length}
    <ul class="errors" role="alert">
      {#each validation.errors as error}
        <li>{error}</li>
      {/each}
    </ul>
  {/if}
  <ol>
    {#each steps as step}
      <li>
        <strong>{step.label}</strong>
        <span>{step.kind} · {step.ready ? 'ready' : 'not ready'}</span>
      </li>
    {/each}
  </ol>
</section>

<style>
  .preview {
    border: 1px solid var(--border-default, #334155);
    border-radius: 8px;
    padding: 12px;
  }

  header {
    display: flex;
    justify-content: space-between;
    gap: 10px;
  }

  h2,
  ol,
  ul {
    margin: 0;
    letter-spacing: 0;
  }

  h2 {
    font-size: 16px;
  }

  span,
  li {
    font-size: 12px;
  }

  header span {
    color: var(--warning);
  }

  header span.pass {
    color: var(--success);
  }

  ol,
  ul {
    display: grid;
    gap: 8px;
    margin-top: 12px;
    padding-left: 18px;
  }

  li span {
    display: block;
    color: var(--text-muted, #94a3b8);
  }

  .errors {
    color: var(--danger);
  }
</style>
