<script>
  import { createWorkbenchConversation, workbenchKernelRequest } from '$lib/api.js';

  let { projectId = null, onContinue = () => {} } = $props();

  let inputText = $state('');
  let searchQuery = $state('');
  let searchResults = $state([]);
  let status = $state('');
  let saving = $state(false);
  let searching = $state(false);

  const quickPrompts = [
    'Talk through an idea',
    'Compare a few options',
    'Draft a note',
  ];

  async function saveConversation() {
    const text = inputText.trim();
    if (!text || saving) return;
    saving = true;
    status = '';
    try {
      await createWorkbenchConversation({
        route_kind: 'save_transcript',
        user_text: text,
        project_id: projectId,
      });
      status = 'Save requested';
    } catch (err) {
      status = `Save blocked: ${err.message}`;
    } finally {
      saving = false;
    }
  }

  function exportConversation(format = 'md') {
    if (!projectId) {
      status = 'Export requires an active project.';
      return;
    }
    const encodedProject = encodeURIComponent(projectId);
    const encodedFormat = encodeURIComponent(format);
    window.location.assign(`/api/v1/chat/export/${encodedProject}?format=${encodedFormat}`);
  }

  async function searchConversation() {
    const query = searchQuery.trim();
    if (!query || searching) return;
    searching = true;
    status = '';
    searchResults = [];
    try {
      const params = new URLSearchParams({ q: query, limit: '10' });
      if (projectId) params.set('project_id', projectId);
      const payload = await workbenchKernelRequest(`/api/v1/chat/search?${params.toString()}`);
      searchResults = payload.results ?? [];
      status = `${payload.total ?? searchResults.length} conversation matches`;
    } catch (err) {
      status = `Search blocked: ${err.message}`;
    } finally {
      searching = false;
    }
  }
</script>

<main class="conversation-front-door" data-view="workbench-conversation" data-testid="workbench-conversation-front-door">
  <section class="conversation-header" aria-label="Workbench conversation">
    <div>
      <h1>Conversation</h1>
      <p>No project setup required.</p>
    </div>
    <span class="mode-pill">Casual</span>
  </section>

  <section class="conversation-panel" aria-label="Casual conversation composer">
    <div class="prompt-row" aria-label="Conversation starters">
      {#each quickPrompts as prompt}
        <button type="button" onclick={() => { inputText = prompt; }}>
          {prompt}
        </button>
      {/each}
    </div>

    <textarea
      bind:value={inputText}
      aria-label="Conversation message"
      placeholder="Start with a normal question, half-formed idea, or rough note."
      rows="7"
    ></textarea>

    <div class="action-row">
      <button type="button" class="primary-action" disabled={!inputText.trim()} onclick={() => onContinue(inputText)}>
        Continue
      </button>
      <button type="button" disabled={!inputText.trim() || saving} onclick={saveConversation}>
        <i class={`fas ${saving ? 'fa-spinner fa-spin' : 'fa-floppy-disk'}`} aria-hidden="true"></i>
        <span>Save</span>
      </button>
      <button type="button" disabled={!projectId || !inputText.trim()} aria-label="Promote conversation to proof context" title="Requires a project and explicit proof context">
        <i class="fas fa-arrow-up-right-from-square" aria-hidden="true"></i>
        <span>Promote</span>
      </button>
    </div>

    {#if status}
      <p class="status-line" role="status">{status}</p>
    {/if}
  </section>

  <section class="conversation-panel" aria-label="Conversation history tools">
    <div class="history-actions">
      <button type="button" disabled={!projectId} onclick={() => exportConversation('md')}>
        <i class="fas fa-file-export" aria-hidden="true"></i>
        <span>Export Markdown</span>
      </button>
      <button type="button" disabled={!projectId} onclick={() => exportConversation('json')}>
        <i class="fas fa-code" aria-hidden="true"></i>
        <span>Export JSON</span>
      </button>
      <label class="search-control">
        <span class="sr-only">Search conversation history</span>
        <input
          bind:value={searchQuery}
          type="search"
          placeholder="Search history"
          onkeydown={(event) => {
            if (event.key === 'Enter') searchConversation();
          }}
        />
      </label>
      <button type="button" disabled={!searchQuery.trim() || searching} onclick={searchConversation}>
        <i class={`fas ${searching ? 'fa-spinner fa-spin' : 'fa-search'}`} aria-hidden="true"></i>
        <span>Search</span>
      </button>
    </div>

    {#if searchResults.length}
      <ul class="search-results" aria-label="Conversation search results">
        {#each searchResults as result}
          <li>
            <strong>{result.project_id}</strong>
            <span>{result.role}</span>
            <p>{result.snippet}</p>
          </li>
        {/each}
      </ul>
    {/if}
  </section>
</main>

<style>
  .conversation-front-door {
    display: grid;
    gap: 14px;
    max-width: 1080px;
    padding: 16px;
    color: var(--text-primary, #e5e7eb);
  }

  .conversation-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 12px;
  }

  h1,
  p {
    margin: 0;
  }

  h1 {
    font-size: 1.35rem;
  }

  .conversation-header p,
  .status-line {
    margin-top: 4px;
    color: var(--text-muted, #94a3b8);
    font-size: 0.85rem;
  }

  .mode-pill {
    border: 1px solid var(--border-default, #334155);
    border-radius: 999px;
    padding: 5px 10px;
    background: var(--surface-elevated, #111827);
    font-size: 0.76rem;
    font-weight: 700;
  }

  .conversation-panel {
    display: grid;
    gap: 12px;
    border: 1px solid var(--border-default, #334155);
    border-radius: 8px;
    background: var(--surface-elevated, #111827);
    padding: 14px;
  }

  .prompt-row {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }

  button {
    min-height: 44px;
    border: 1px solid var(--border-default, #334155);
    border-radius: 6px;
    background: var(--surface-secondary, #1f2937);
    color: var(--text-primary, #e5e7eb);
    font: inherit;
    font-size: 0.82rem;
    font-weight: 700;
    cursor: pointer;
  }

  button:disabled {
    cursor: not-allowed;
    opacity: 0.55;
  }

  button:not(:disabled):hover,
  button:not(:disabled):focus-visible {
    border-color: var(--primary, #38bdf8);
    outline: 2px solid rgba(56, 189, 248, 0.24);
    outline-offset: 2px;
  }

  textarea {
    width: 100%;
    min-height: 180px;
    resize: vertical;
    box-sizing: border-box;
    border: 1px solid var(--border-default, #334155);
    border-radius: 8px;
    background: var(--surface-primary, #0f172a);
    color: var(--text-primary, #e5e7eb);
    font: inherit;
    line-height: 1.5;
    padding: 12px;
  }

  input:focus-visible,
  textarea:focus-visible {
    border-color: var(--primary, #38bdf8);
    outline: 2px solid rgba(56, 189, 248, 0.3);
    outline-offset: 2px;
  }

  .action-row {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    justify-content: flex-end;
  }

  .history-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
  }

  .search-control {
    flex: 1 1 220px;
  }

  .search-control input {
    width: 100%;
    box-sizing: border-box;
    border: 1px solid var(--border-default, #334155);
    border-radius: 6px;
    background: var(--surface-primary, #0f172a);
    color: var(--text-primary, #e5e7eb);
    font: inherit;
    padding: 8px 10px;
  }

  .search-results {
    display: grid;
    gap: 8px;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .search-results li {
    border: 1px solid var(--border-default, #334155);
    border-radius: 6px;
    padding: 10px;
  }

  .search-results span {
    margin-left: 8px;
    color: var(--text-muted, #94a3b8);
    font-size: 0.78rem;
  }

  .search-results p {
    margin-top: 6px;
  }

  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
  }

  .action-row button {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    padding: 0 12px;
  }

  .primary-action {
    background: var(--primary, #2563eb);
    border-color: var(--primary, #2563eb);
  }
</style>
