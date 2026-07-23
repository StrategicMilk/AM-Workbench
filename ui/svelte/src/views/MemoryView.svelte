<script>
  /**
   * Memory browser — search, filter, and manage memory entries.
   *
   * Provides full-text search, session filtering, entry expansion,
   * and the ability to add new entries manually.
   */
  import * as api from '$lib/api.js';
  import * as fmt from '$lib/utils/format.js';
  import { showToast } from '$lib/stores/toast.svelte.js';
  import HelpPopover from '$lib/components/help/HelpPopover.svelte';
  import HelpTooltip from '$lib/components/help/HelpTooltip.svelte';

  // -- State -------------------------------------------------------------------

  let entries = $state([]);
  let sessions = $state([]);
  let stats = $state(null);
  let loading = $state(true);
  let searchQuery = $state('');
  let searchPending = $state(false);
  let selectedSession = $state('');
  let expandedId = $state(null);
  let showAddForm = $state(false);
  let actionPending = $state(false);

  let newEntry = $state({
    type: 'feedback',
    title: '',
    content: '',
    session_id: '',
  });

  // -- Derived -----------------------------------------------------------------

  let filteredEntries = $derived(
    selectedSession
      ? entries.filter((e) => (e.session_id ?? e.session) === selectedSession)
      : entries
  );

  let entryCount = $derived(filteredEntries.length);

  // -- Data loading ------------------------------------------------------------

  async function loadAll() {
    loading = true;
    try {
      const [entriesData, sessionsData, statsData] = await Promise.all([
        api.getMemoryEntries().catch(() => ({ items: [] })),
        api.getMemorySessions().catch(() => ({ items: [] })),
        api.getMemoryStats().catch(() => null),
      ]);
      entries = entriesData?.items ?? (Array.isArray(entriesData) ? entriesData : []);
      sessions = sessionsData?.items ?? (Array.isArray(sessionsData) ? sessionsData : []);
      stats = statsData;
    } catch (err) {
      showToast(`Failed to load memory: ${err.message}`, 'error');
    } finally {
      loading = false;
    }
  }

  $effect(() => { loadAll(); });

  // -- Search ------------------------------------------------------------------

  let searchTimeout;

  function handleSearchInput() {
    clearTimeout(searchTimeout);
    if (!searchQuery.trim()) {
      loadAll();
      return;
    }
    searchTimeout = setTimeout(doSearch, 350);
  }

  async function doSearch() {
    searchPending = true;
    try {
      const result = await api.searchMemory(searchQuery);
      entries = result?.entries ?? result?.results ?? (Array.isArray(result) ? result : []);
    } catch (err) {
      showToast(`Search failed: ${err.message}`, 'error');
    } finally {
      searchPending = false;
    }
  }

  // -- Actions -----------------------------------------------------------------

  async function handleDelete(entryId) {
    if (!confirm('Delete this memory entry?')) return;
    try {
      await api.deleteMemoryEntry(entryId);
      entries = entries.filter((e) => (e.id ?? e.entry_id) !== entryId);
      showToast('Entry deleted', 'info');
      if (stats) {
        stats = { ...stats, total_entries: (stats.total_entries ?? 1) - 1 };
      }
    } catch (err) {
      showToast(`Delete failed: ${err.message}`, 'error');
    }
  }

  async function handleAddEntry(e) {
    e.preventDefault();
    if (!newEntry.title.trim() || !newEntry.content.trim()) {
      showToast('Title and content are required', 'warning');
      return;
    }
    actionPending = true;
    try {
      const result = await api.addMemoryEntry(newEntry);
      entries = [result, ...entries];
      showToast('Memory entry added', 'success');
      showAddForm = false;
      newEntry = { type: 'feedback', title: '', content: '', session_id: '' };
      await loadAll();
    } catch (err) {
      showToast(`Failed to add entry: ${err.message}`, 'error');
    } finally {
      actionPending = false;
    }
  }

  function toggleExpand(id) {
    expandedId = expandedId === id ? null : id;
  }

  function entryTypeColor(type) {
    const map = {
      feedback: 'primary',
      project: 'secondary',
      reference: 'info',
      error: 'danger',
    };
    return map[type] ?? 'muted';
  }
</script>

<div class="memory-view">
  <div class="view-header">
    <h2>
      <i class="fas fa-brain"></i>
      Memory
    </h2>
    <HelpPopover
      title="Memory"
      body="Persistent memory entries for this project. Memories are injected into agent context at the start of each run, giving Foreman and Worker access to standing facts, preferences, and learned constraints. Entry types: fact (always injected), preference (injected when relevant), constraint (hard rule enforced by Inspector), and episodic (recent event used for continuity). Entries are deduplicated by key — adding a new entry with the same key replaces the previous value."
      severity="info"
    />
    <div class="header-actions">
      <button
        class="btn btn-primary btn-sm"
        onclick={() => { showAddForm = !showAddForm; }}
        aria-expanded={showAddForm}
        aria-label="Add memory entry"
      >
        <i class="fas fa-plus"></i>
        Add Entry
      </button>
      <button
        class="btn btn-secondary btn-sm"
        onclick={loadAll}
        disabled={loading}
        aria-label="Refresh memory entries"
      >
        <i class="fas fa-sync-alt" class:fa-spin={loading}></i>
      </button>
    </div>
  </div>

  <!-- Add entry form -->
  {#if showAddForm}
    <section class="card add-form-card" aria-label="Add memory entry form">
      <h3 class="form-title"><i class="fas fa-plus-circle"></i> New Memory Entry</h3>
      <form onsubmit={handleAddEntry} class="add-form" aria-label="New memory entry">
        <div class="form-row">
          <label class="form-group">
            <span class="form-label">Type</span>
            <select class="input" bind:value={newEntry.type} aria-label="Entry type">
              <option value="feedback">Feedback</option>
              <option value="project">Project</option>
              <option value="reference">Reference</option>
              <option value="note">Note</option>
            </select>
          </label>
          <label class="form-group" style="flex: 1">
            <span class="form-label">Title</span>
            <input
              type="text"
              class="input"
              bind:value={newEntry.title}
              placeholder="Entry title..."
              required
              aria-required="true"
              aria-label="Entry title"
            />
          </label>
          <label class="form-group">
            <span class="form-label">Session ID (optional)</span>
            <input
              type="text"
              class="input"
              bind:value={newEntry.session_id}
              placeholder="session-id..."
              aria-label="Session ID"
            />
          </label>
        </div>
        <label class="form-group">
          <span class="form-label">Content</span>
          <textarea
            class="input textarea"
            bind:value={newEntry.content}
            rows="4"
            placeholder="Memory content..."
            required
            aria-required="true"
            aria-label="Entry content"
          ></textarea>
        </label>
        <div class="form-actions">
          <button type="submit" class="btn btn-primary" disabled={actionPending} aria-label="Save memory entry">
            <i class="fas fa-save"></i> Save Entry
          </button>
          <button
            type="button"
            class="btn btn-secondary"
            onclick={() => { showAddForm = false; }}
            aria-label="Cancel adding entry"
          >
            Cancel
          </button>
        </div>
      </form>
    </section>
  {/if}

  <div class="memory-layout">
    <!-- Main content -->
    <div class="memory-main">
      <!-- Search + filter bar -->
      <div class="search-bar" role="search">
        <div class="search-input-wrap">
          <i class="fas fa-search search-icon" aria-hidden="true"></i>
          <input
            type="search"
            class="input search-input"
            bind:value={searchQuery}
            oninput={handleSearchInput}
            placeholder="Search memory entries..."
            aria-label="Search memory entries"
          />
          {#if searchPending}
            <i class="fas fa-spinner fa-spin search-spinner" aria-hidden="true"></i>
          {/if}
        </div>
        <label class="form-group filter-select">
          <span class="sr-only">Filter by session</span>
          <select
            class="input"
            bind:value={selectedSession}
            aria-label="Filter by session"
          >
            <option value="">All sessions</option>
            {#each sessions as session (session.id ?? session)}
              <option value={session.id ?? session}>{session.label ?? session.id ?? session}</option>
            {/each}
          </select>
        </label>
      </div>

      {#if loading}
        <div class="loading-state" role="status" aria-live="polite">
          <i class="fas fa-spinner fa-spin"></i>
          Loading memory entries...
        </div>
      {:else if filteredEntries.length === 0}
        <div class="empty-state">
          <i class="fas fa-brain"></i>
          <p>{searchQuery ? 'No entries match your search.' : 'No memory entries yet.'}</p>
          {#if searchQuery}
            <button class="btn btn-secondary btn-sm" onclick={() => { searchQuery = ''; loadAll(); }}>
              Clear search
            </button>
          {/if}
        </div>
      {:else}
        <div class="entry-count" role="status" aria-live="polite">
          Showing {entryCount} {entryCount === 1 ? 'entry' : 'entries'}
          {#if selectedSession} in session {selectedSession}{/if}
        </div>
        <ul class="entry-list" aria-label="Memory entries">
          {#each filteredEntries as entry (entry.id ?? entry.entry_id)}
            {@const entryId = entry.id ?? entry.entry_id}
            <li class="entry-item" class:expanded={expandedId === entryId}>
              <div class="entry-header">
                <div class="entry-meta">
                  <span class="entry-type status-badge status-{entryTypeColor(entry.type)}">
                    {entry.type ?? 'note'}
                  </span>
                  <button
                    class="entry-title-btn"
                    onclick={() => toggleExpand(entryId)}
                    aria-expanded={expandedId === entryId}
                    aria-label="Toggle entry: {entry.title}"
                  >
                    {entry.title ?? 'Untitled'}
                  </button>
                </div>
                <div class="entry-actions">
                  <span class="entry-date">{fmt.relativeTime(entry.created_at ?? entry.timestamp)}</span>
                  <button
                    class="btn-icon btn-danger-icon"
                    onclick={() => handleDelete(entryId)}
                    aria-label="Delete entry: {entry.title}"
                    title="Delete entry"
                  >
                    <i class="fas fa-trash-alt"></i>
                  </button>
                </div>
              </div>

              {#if expandedId === entryId}
                <div class="entry-body" role="region" aria-label="Entry content: {entry.title}">
                  {#if entry.session_id}
                    <p class="entry-session">
                      <i class="fas fa-tag"></i>
                      Session: {entry.session_id}
                    </p>
                  {/if}
                  <div class="entry-content">{entry.content ?? entry.body ?? ''}</div>
                  {#if entry.metadata && Object.keys(entry.metadata).length > 0}
                    <details class="entry-metadata">
                      <summary>Metadata</summary>
                      <dl class="meta-list">
                        {#each Object.entries(entry.metadata) as [k, v]}
                          <dt>{k}</dt>
                          <dd>{typeof v === 'object' ? JSON.stringify(v) : String(v)}</dd>
                        {/each}
                      </dl>
                    </details>
                  {/if}
                </div>
              {/if}
            </li>
          {/each}
        </ul>
      {/if}
    </div>

    <!-- Stats sidebar -->
    <aside class="memory-sidebar" aria-label="Memory statistics">
      <section class="card stats-card">
        <h3 class="sidebar-section-title"><i class="fas fa-chart-bar"></i> Stats</h3>
        {#if stats}
          <dl class="stats-list">
            <dt>Total entries</dt>
            <dd>{fmt.integer(stats.total_entries ?? 0)}</dd>
            <dt>Sessions</dt>
            <dd>{fmt.integer(stats.total_sessions ?? sessions.length)}</dd>
            <dt>Searches run</dt>
            <dd>{fmt.integer(stats.search_count ?? 0)}</dd>
            <dt>Oldest entry</dt>
            <dd>{fmt.relativeTime(stats.oldest_entry)}</dd>
            <dt>Latest entry</dt>
            <dd>{fmt.relativeTime(stats.latest_entry)}</dd>
          </dl>
        {:else}
          <p class="text-muted">Stats unavailable.</p>
        {/if}
      </section>

      <section class="card sessions-card">
        <h3 class="sidebar-section-title"><i class="fas fa-layer-group"></i> Sessions</h3>
        {#if sessions.length === 0}
          <p class="text-muted">No sessions recorded.</p>
        {:else}
          <ul class="session-list" aria-label="Memory sessions">
            {#each sessions as session (session.id ?? session)}
              {@const sid = session.id ?? session}
              <li>
                <button
                  class="session-btn"
                  class:active={selectedSession === sid}
                  onclick={() => { selectedSession = selectedSession === sid ? '' : sid; }}
                  aria-pressed={selectedSession === sid}
                  aria-label="Filter by session {sid}"
                >
                  <span class="session-id">{session.label ?? sid}</span>
                  {#if session.count != null}
                    <span class="session-count">{session.count}</span>
                  {/if}
                </button>
              </li>
            {/each}
          </ul>
        {/if}
      </section>
    </aside>
  </div>
</div>

<style>
  .memory-view {
    padding: 24px;
    max-width: 1400px;
  }

  .view-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 20px;
    gap: 12px;
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

  .view-header h2 i { color: var(--primary); }

  .header-actions { display: flex; gap: 8px; }

  /* Card */
  .card {
    background: var(--surface-elevated);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-lg);
    padding: 16px;
  }

  .add-form-card { margin-bottom: 20px; }

  .form-title {
    font-size: 0.9375rem;
    font-weight: 600;
    color: var(--text-primary);
    margin: 0 0 14px 0;
    display: flex;
    align-items: center;
    gap: 8px;
  }

  /* Forms */
  .add-form { display: flex; flex-direction: column; gap: 12px; }

  .form-row {
    display: flex;
    gap: 12px;
    flex-wrap: wrap;
  }

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

  .form-actions { display: flex; gap: 8px; }

  /* Layout */
  .memory-layout {
    display: grid;
    grid-template-columns: 1fr 260px;
    gap: 20px;
    align-items: start;
  }

  /* Search */
  .search-bar {
    display: flex;
    gap: 10px;
    margin-bottom: 16px;
  }

  .search-input-wrap {
    flex: 1;
    position: relative;
  }

  .search-icon {
    position: absolute;
    left: 10px;
    top: 50%;
    transform: translateY(-50%);
    color: var(--text-muted);
    pointer-events: none;
    font-size: 0.8125rem;
  }

  .search-input { padding-left: 32px; }

  .search-spinner {
    position: absolute;
    right: 10px;
    top: 50%;
    transform: translateY(-50%);
    color: var(--text-muted);
    font-size: 0.8125rem;
  }

  .filter-select { flex-shrink: 0; min-width: 160px; }

  .loading-state {
    display: flex;
    align-items: center;
    gap: 10px;
    color: var(--text-muted);
    padding: 48px 24px;
  }

  .empty-state {
    text-align: center;
    padding: 48px 24px;
    color: var(--text-muted);
    font-size: 0.9375rem;
  }

  .empty-state i {
    font-size: 2rem;
    margin-bottom: 10px;
    display: block;
    opacity: 0.4;
  }

  .entry-count {
    font-size: 0.8125rem;
    color: var(--text-muted);
    margin-bottom: 10px;
  }

  /* Entry list */
  .entry-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .entry-item {
    background: var(--surface-elevated);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-md);
    overflow: hidden;
    transition: border-color var(--transition-base);
  }

  .entry-item.expanded { border-color: var(--primary); }

  .entry-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 10px 14px;
    gap: 10px;
  }

  .entry-meta {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
  }

  .entry-title-btn {
    background: none;
    border: none;
    cursor: pointer;
    font-size: 0.875rem;
    font-weight: 500;
    color: var(--text-primary);
    font-family: inherit;
    text-align: left;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .entry-title-btn:hover { color: var(--primary); }

  .entry-actions {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-shrink: 0;
  }

  .entry-date {
    font-size: 0.75rem;
    color: var(--text-muted);
  }

  .btn-icon {
    background: none;
    border: none;
    cursor: pointer;
    color: var(--text-muted);
    padding: 4px 6px;
    border-radius: var(--radius-sm);
    font-size: 0.8125rem;
  }

  .btn-icon:hover { background: var(--surface-hover); }
  .btn-danger-icon:hover { color: var(--danger); background: var(--danger-muted); }

  .entry-body {
    padding: 10px 14px 14px;
    border-top: 1px solid var(--border-subtle);
  }

  .entry-session {
    font-size: 0.75rem;
    color: var(--text-muted);
    margin: 0 0 8px 0;
    display: flex;
    align-items: center;
    gap: 5px;
  }

  .entry-content {
    font-size: 0.875rem;
    color: var(--text-secondary);
    line-height: var(--leading-relaxed);
    white-space: pre-wrap;
    word-break: break-word;
  }

  .entry-metadata {
    margin-top: 10px;
    font-size: 0.8125rem;
  }

  .entry-metadata summary {
    cursor: pointer;
    color: var(--text-muted);
    font-weight: 500;
    margin-bottom: 6px;
  }

  .meta-list {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: 4px 12px;
    margin: 0;
  }

  .meta-list dt { color: var(--text-muted); }
  .meta-list dd { margin: 0; color: var(--text-primary); font-family: var(--font-mono); font-size: 0.75rem; word-break: break-all; }

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
  .status-secondary { background: var(--secondary-muted); color: var(--secondary); }
  .status-info { background: var(--info-muted); color: var(--info); }
  .status-danger { background: var(--danger-muted); color: var(--danger); }
  .status-muted { background: var(--surface-hover); color: var(--text-muted); }

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
    transition: background var(--transition-base);
  }

  .btn:disabled { opacity: 0.5; cursor: not-allowed; }
  .btn-sm { padding: 6px 10px; font-size: 0.8125rem; }

  .btn-primary { background: var(--primary); color: var(--text-on-primary); }
  .btn-primary:hover:not(:disabled) { background: var(--primary-hover); }

  .btn-secondary { background: var(--surface-hover); color: var(--text-primary); border: 1px solid var(--border-default); }
  .btn-secondary:hover:not(:disabled) { background: var(--surface-pressed); }

  /* Sidebar */
  .memory-sidebar { display: flex; flex-direction: column; gap: 16px; }

  .sidebar-section-title {
    font-size: 0.875rem;
    font-weight: 600;
    color: var(--text-primary);
    margin: 0 0 12px 0;
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .sidebar-section-title i { color: var(--text-muted); }

  .stats-list {
    display: grid;
    grid-template-columns: 1fr auto;
    gap: 6px 8px;
    margin: 0;
    font-size: 0.8125rem;
  }

  .stats-list dt { color: var(--text-muted); }
  .stats-list dd { color: var(--text-primary); font-weight: 500; margin: 0; text-align: right; }

  .session-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 4px; }

  .session-btn {
    width: 100%;
    display: flex;
    justify-content: space-between;
    align-items: center;
    background: none;
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    padding: 6px 10px;
    cursor: pointer;
    font-size: 0.8125rem;
    font-family: inherit;
    color: var(--text-secondary);
    transition: background var(--transition-base), color var(--transition-base);
    text-align: left;
  }

  .session-btn:hover { background: var(--surface-hover); color: var(--text-primary); }
  .session-btn.active { background: var(--primary-muted); color: var(--primary); border-color: var(--primary); }

  .session-id { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .session-count { font-size: 0.75rem; color: var(--text-muted); flex-shrink: 0; }

  .text-muted { color: var(--text-muted); font-size: 0.875rem; }

  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0,0,0,0);
    white-space: nowrap;
    border: 0;
  }

  @media (max-width: 1024px) {
    .memory-layout { grid-template-columns: 1fr; }
  }

  @media (max-width: 600px) {
    .search-bar { flex-direction: column; }
    .filter-select { min-width: unset; }
  }
</style>
