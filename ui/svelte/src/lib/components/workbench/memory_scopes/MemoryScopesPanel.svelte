<script>
  import * as api from '$lib/api.js';

  let { projectId = 'default', selectedScopeId = '', onScopeChange = null } = $props();

  const fallbackScopes = [
    { id: 'private_personal', label: 'Private Personal', policy: 'Explicit save' },
    { id: 'conversation_only', label: 'Conversation Only', policy: 'Session scoped' },
    { id: 'creative', label: 'Creative', policy: 'Durable creative context' },
    { id: 'professional', label: 'Professional', policy: 'Work context' },
    { id: 'sensitive', label: 'Sensitive', policy: 'Explicit save and promotion' },
    { id: 'project', label: 'Project', policy: 'Structured project memory' },
  ];

  let scopes = $state([]);
  let activeScopeId = $state(selectedScopeId);
  let scopeState = $state('loading');
  let scopeError = $state('');

  function normalizeScopes(result) {
    const rows = Array.isArray(result?.scopes) ? result.scopes : [];
    return rows.map((scope) => ({
      id: String(scope.id ?? scope.scope_id ?? scope.label ?? ''),
      label: String(scope.label ?? scope.name ?? scope.id ?? 'Unnamed scope'),
      policy: String(scope.policy ?? scope.description ?? scope.mode ?? 'No policy returned'),
      selected: Boolean(scope.selected),
    })).filter((scope) => scope.id);
  }

  function selectScope(scope) {
    activeScopeId = scope.id;
    onScopeChange?.({ project_id: projectId, scope_id: scope.id, source: scopeState });
  }

  $effect(() => {
    let cancelled = false;
    api.getMemoryScopes(projectId)
      .then((result) => {
        if (cancelled) return;
        const nextScopes = normalizeScopes(result);
        if (nextScopes.length === 0) {
          scopes = fallbackScopes.map((scope) => ({ ...scope, degraded: true }));
          scopeState = 'blocked';
          scopeError = 'memory_scopes_empty';
          return;
        }
        scopes = nextScopes.map((scope) => ({ ...scope, degraded: false }));
        activeScopeId = selectedScopeId || nextScopes.find((scope) => scope.selected)?.id || nextScopes[0].id;
        scopeState = 'api';
        scopeError = '';
      })
      .catch((error) => {
        if (!cancelled) {
          scopes = fallbackScopes.map((scope) => ({ ...scope, degraded: true }));
          activeScopeId = selectedScopeId || fallbackScopes[0].id;
          scopeState = 'blocked';
          scopeError = `memory_scopes_unavailable:${error?.message ?? 'unknown'}`;
        }
      });
    return () => {
      cancelled = true;
    };
  });
</script>

<section class="memory-scopes" aria-labelledby="memory-scopes-title" data-project-id={projectId} data-scope-state={scopeState}>
  <header>
    <h1 id="memory-scopes-title">Memory Scopes</h1>
    <p>Choose how recall, deletion, review, decay, and cross-scope use should work for this conversation.</p>
  </header>
  {#if scopeError}
    <p class="scope-error" role="alert">{scopeError}</p>
  {/if}
  <div class="scope-grid">
    {#each scopes as scope}
      <article data-selected={activeScopeId === scope.id} data-degraded={scope.degraded}>
        <h2>{scope.label}</h2>
        <p>{scope.policy}</p>
        <button
          type="button"
          onclick={() => selectScope(scope)}
          aria-pressed={activeScopeId === scope.id}
          aria-label={`Select memory scope ${scope.label}`}
        >
          Select
        </button>
      </article>
    {/each}
  </div>
</section>

<style>
  .memory-scopes {
    display: grid;
    gap: 20px;
    padding: 24px;
  }

  h1 {
    margin: 0 0 8px;
    font-size: 28px;
  }

  p {
    margin: 0;
    color: var(--text-secondary, #4b5563);
  }

  .scope-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
    gap: 12px;
  }

  article {
    min-height: 104px;
    padding: 14px;
    border: 1px solid var(--border-color, #d1d5db);
    border-radius: 8px;
    background: var(--surface-primary, #fff);
  }

  article[data-selected='true'] {
    border-color: var(--accent-color, #2563eb);
  }

  article[data-degraded='true'],
  .scope-error {
    border-color: var(--warning, #b45309);
    color: var(--warning, #b45309);
  }

  button {
    margin-top: 10px;
  }

  h2 {
    margin: 0 0 8px;
    font-size: 17px;
  }
</style>
