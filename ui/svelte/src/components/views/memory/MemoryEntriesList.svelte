<script>
  import Icon from '$lib/a11y/Icon.svelte';
  import * as fmt from '$lib/utils/format.js';

  let {
    filteredEntries = [],
    entryCount = 0,
    selectedSession = '',
    searchQuery = '',
    loading = false,
    expandedId = null,
    onClearSearch,
    onToggleExpand,
    onDelete,
    entryTypeColor,
  } = $props();

  const MAX_RENDERED_ENTRIES = 200;
  let visibleEntries = $derived(filteredEntries.slice(0, MAX_RENDERED_ENTRIES));
  let hiddenEntryCount = $derived(Math.max(0, filteredEntries.length - visibleEntries.length));
</script>

{#if loading}
  <div class="loading-state" role="status" aria-live="polite">
    <Icon name="spinner" class="fa-spin" />
    Loading memory entries...
  </div>
{:else if filteredEntries.length === 0}
  <div class="empty-state">
    <Icon name="brain" />
    <p>{searchQuery ? 'No entries match your search.' : 'No memory entries yet.'}</p>
    {#if searchQuery}
      <button class="btn btn-secondary btn-sm" onclick={onClearSearch}>
        Clear search
      </button>
    {/if}
  </div>
{:else}
  <div class="entry-count" role="status" aria-live="polite">
    Showing {visibleEntries.length} of {entryCount} {entryCount === 1 ? 'entry' : 'entries'}
    {#if selectedSession} in session {selectedSession}{/if}
    {#if hiddenEntryCount > 0}
      <span class="entry-count-limit">First {MAX_RENDERED_ENTRIES} displayed</span>
    {/if}
  </div>
  <ul class="entry-list" aria-label="Memory entries">
    {#each visibleEntries as entry (entry.id ?? entry.entry_id)}
      {@const entryId = entry.id ?? entry.entry_id}
      <li class="entry-item" class:expanded={expandedId === entryId}>
        <div class="entry-header">
          <div class="entry-meta">
            <span class="entry-type status-badge status-{entryTypeColor(entry.type)}">
              {entry.type ?? 'note'}
            </span>
            <button
              class="entry-title-btn"
              onclick={() => onToggleExpand(entryId)}
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
              onclick={() => onDelete(entryId)}
              aria-label="Delete entry: {entry.title}"
              title="Delete entry"
            >
              <Icon name="trash-alt" />
            </button>
          </div>
        </div>

        {#if expandedId === entryId}
          <div class="entry-body" role="region" aria-label="Entry content: {entry.title}">
            {#if entry.session_id}
              <p class="entry-session">
                <Icon name="tag" />
                Session: {entry.session_id}
              </p>
            {/if}
            <div class="entry-content">{entry.content ?? entry.body ?? ''}</div>
            {#if entry.metadata && Object.keys(entry.metadata).length > 0}
              <details class="entry-metadata">
                <summary>Metadata</summary>
                <dl class="meta-list">
                  {#each Object.entries(entry.metadata) as [key, value]}
                    <dt>{key}</dt>
                    <dd>{typeof value === 'object' ? JSON.stringify(value) : String(value)}</dd>
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

<style>
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

  .entry-count-limit {
    display: inline-block;
    margin-left: 8px;
    color: var(--warning, #b45309);
    font-weight: 600;
  }

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
    min-width: 44px;
    min-height: 44px;
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

  .btn-sm {
    padding: 6px 10px;
    font-size: 0.8125rem;
  }

  .btn-secondary { background: var(--surface-hover); color: var(--text-primary); border: 1px solid var(--border-default); }
  .btn-secondary:hover:not(:disabled) { background: var(--surface-pressed); }
</style>
