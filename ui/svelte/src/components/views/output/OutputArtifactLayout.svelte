<script>
  import Icon from '$lib/a11y/Icon.svelte';
  import * as fmt from '$lib/utils/format.js';

  let {
    review = null,
    files = [],
    versions = [],
    activeFile = null,
    selectedFile = null,
    selectedVersion = null,
    codeContent = '',
    codeLanguage = 'text',
    fileIcon,
    onSelectFile,
    onSelectVersion,
    onCopyCode,
    onCodeElementChange,
  } = $props();

  let codeElement = $state(null);

  function iconName(iconClass) {
    return String(iconClass ?? '')
      .split(/\s+/)
      .find((token) => token.startsWith('fa-') && token !== 'fas' && token !== 'far' && token !== 'fab')
      ?.slice(3) ?? 'file';
  }

  function moveListboxSelection(event, items, currentItem, getKey, selectItem, idPrefix) {
    const currentIndex = items.findIndex((item) => getKey(item) === getKey(currentItem));
    if (currentIndex === -1) return;

    let nextIndex = currentIndex;
    if (event.key === 'ArrowDown' || event.key === 'ArrowRight') {
      nextIndex = (currentIndex + 1) % items.length;
    } else if (event.key === 'ArrowUp' || event.key === 'ArrowLeft') {
      nextIndex = (currentIndex - 1 + items.length) % items.length;
    } else if (event.key === 'Home') {
      nextIndex = 0;
    } else if (event.key === 'End') {
      nextIndex = items.length - 1;
    } else {
      return;
    }

    event.preventDefault();
    const nextItem = items[nextIndex];
    selectItem(nextItem);
    document.getElementById(`${idPrefix}-${nextIndex}`)?.focus();
  }

  $effect(() => {
    onCodeElementChange(codeElement);
  });
</script>

<div class="output-layout" aria-live="polite">
  {#if files.length > 1}
    <nav class="file-tree" aria-label="Output file tree">
      <h3 class="tree-title">
        <Icon name="folder-tree" />
        Files
        <span class="badge">{files.length}</span>
      </h3>
      <ul class="file-list" role="listbox" aria-label="Select a file">
        {#each files as file (file.name)}
          <li>
            {@const fileIndex = files.findIndex((candidate) => candidate.name === file.name)}
            <button
              id={`output-file-option-${fileIndex}`}
              class="file-btn"
              class:active={selectedFile === file.name}
              onclick={() => onSelectFile(file.name)}
              onkeydown={(event) => moveListboxSelection(event, files, file, (item) => item?.name, (item) => onSelectFile(item.name), 'output-file-option')}
              role="option"
              aria-selected={selectedFile === file.name}
              tabindex={selectedFile === file.name ? 0 : -1}
              aria-label="View file {file.name}"
            >
              <Icon name={iconName(fileIcon(file.name))} />
              <span class="file-name">{file.name}</span>
              {#if file.size != null}
                <span class="file-size">{fmt.fileSize(file.size)}</span>
              {/if}
            </button>
          </li>
        {/each}
      </ul>
    </nav>
  {/if}

  <div class="code-area" aria-label="Code output">
    {#if activeFile}
      <div class="code-toolbar">
        <div class="code-toolbar-left">
          <Icon name={iconName(fileIcon(activeFile.name))} />
          <span class="code-filename">{activeFile.name}</span>
          <span class="code-lang">{codeLanguage}</span>
        </div>
        <div class="code-toolbar-right">
          {#if activeFile.tokens != null}
            <span class="code-meta">{fmt.integer(activeFile.tokens)} tokens</span>
          {/if}
          {#if activeFile.size != null}
            <span class="code-meta">{fmt.fileSize(activeFile.size)}</span>
          {/if}
          <button
            class="btn btn-secondary btn-sm"
            onclick={onCopyCode}
            aria-label="Copy code to clipboard"
            title="Copy"
          >
            <Icon name="copy" />
          </button>
        </div>
      </div>
      <div class="code-scroll" role="region" aria-label="Code content for {activeFile.name}">
        <pre class="code-pre"><code bind:this={codeElement} class="language-{codeLanguage}">{codeContent}</code></pre>
      </div>
    {/if}
  </div>

  {#if versions.length > 0}
    <aside class="version-history" aria-label="Version history">
      <h3 class="tree-title">
        <Icon name="history" />
        Versions
      </h3>
      <ul class="version-list" role="listbox" aria-label="Output versions">
        {#each versions as version, index ((version.id ?? version.version ?? index))}
          <li>
            <button
              id={`output-version-option-${index}`}
              class="version-btn"
              class:active={selectedVersion === version}
              onclick={() => onSelectVersion(version)}
              onkeydown={(event) => moveListboxSelection(event, versions, version, (item) => item?.id ?? item?.version, onSelectVersion, 'output-version-option')}
              role="option"
              aria-selected={selectedVersion === version}
              tabindex={selectedVersion === version ? 0 : -1}
              aria-label="Version {version.version ?? index + 1}: {version.status ?? ''}"
            >
              <div class="version-row">
                <span class="version-num">v{version.version ?? index + 1}</span>
                <span class="status-badge status-{version.status === 'approved' ? 'success' : version.status === 'rejected' ? 'danger' : 'muted'}">
                  {version.status ?? 'draft'}
                </span>
              </div>
              <div class="version-meta">
                <span>{fmt.relativeTime(version.created_at)}</span>
                {#if version.author}
                  <span>{version.author}</span>
                {/if}
              </div>
            </button>
          </li>
        {/each}
      </ul>
    </aside>
  {/if}
</div>

{#if review.feedback || review.notes}
  <section class="review-notes card" aria-label="Review notes">
    <h3 class="notes-title"><Icon name="comment-alt" /> Inspector Notes</h3>
    <p class="notes-text">{review.feedback ?? review.notes}</p>
  </section>
{/if}

<style>
  .output-layout {
    display: grid;
    grid-template-columns: auto 1fr auto;
    gap: 0;
    flex: 1;
    min-height: 0;
    border: 1px solid var(--border-default);
    border-radius: var(--radius-lg);
    overflow: hidden;
    background: var(--surface-bg);
  }

  .file-tree {
    width: 220px;
    border-right: 1px solid var(--border-default);
    overflow-y: auto;
    background: var(--surface-elevated);
    padding: 12px 0;
  }

  .tree-title {
    font-size: 0.8125rem;
    font-weight: 600;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.06em;
    margin: 0 0 8px 0;
    padding: 0 12px;
    display: flex;
    align-items: center;
    gap: 6px;
  }

  .file-list,
  .version-list {
    list-style: none;
    margin: 0;
    padding: 0;
  }

  .file-btn {
    width: 100%;
    display: flex;
    align-items: center;
    gap: 6px;
    min-height: 44px;
    padding: 6px 12px;
    background: none;
    border: none;
    cursor: pointer;
    font-size: 0.8125rem;
    font-family: var(--font-mono);
    color: var(--text-secondary);
    text-align: left;
    transition: background var(--transition-base), color var(--transition-base);
  }

  .file-btn:hover { background: var(--surface-hover); color: var(--text-primary); }
  .file-btn.active { background: var(--primary-muted); color: var(--primary); }

  .file-name {
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .file-size {
    font-size: 0.6875rem;
    color: var(--text-muted);
    flex-shrink: 0;
  }

  .code-area {
    display: flex;
    flex-direction: column;
    min-width: 0;
    overflow: hidden;
  }

  .code-toolbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 8px 14px;
    border-bottom: 1px solid var(--border-default);
    background: var(--surface-elevated);
    flex-shrink: 0;
  }

  .code-toolbar-left {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 0.875rem;
    color: var(--text-primary);
  }

  .code-filename {
    font-weight: 500;
    font-family: var(--font-mono);
  }

  .code-lang {
    font-size: 0.75rem;
    color: var(--text-muted);
    background: var(--surface-hover);
    padding: 1px 6px;
    border-radius: var(--radius-sm);
  }

  .code-toolbar-right {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .code-meta {
    font-size: 0.75rem;
    color: var(--text-muted);
  }

  .code-scroll {
    flex: 1;
    overflow: auto;
  }

  .code-pre {
    margin: 0;
    padding: 16px 20px;
    font-family: var(--font-mono);
    font-size: 0.8125rem;
    line-height: 1.6;
    min-height: 100%;
    white-space: pre;
    tab-size: 2;
    background: transparent;
  }

  .code-pre code {
    font-family: inherit;
    background: none;
    padding: 0;
    border: none;
  }

  .version-history {
    width: 200px;
    border-left: 1px solid var(--border-default);
    overflow-y: auto;
    background: var(--surface-elevated);
    padding: 12px 0;
  }

  .version-btn {
    width: 100%;
    display: flex;
    flex-direction: column;
    gap: 3px;
    min-height: 44px;
    padding: 8px 12px;
    background: none;
    border: none;
    cursor: pointer;
    font-family: inherit;
    text-align: left;
    transition: background var(--transition-base);
  }

  .version-btn:hover { background: var(--surface-hover); }
  .version-btn.active { background: var(--primary-muted); }

  .version-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 6px;
  }

  .version-num {
    font-weight: 600;
    font-size: 0.875rem;
    color: var(--text-primary);
  }

  .version-meta {
    display: flex;
    flex-direction: column;
    gap: 1px;
    font-size: 0.6875rem;
    color: var(--text-muted);
  }

  .review-notes {
    margin-top: 16px;
    background: var(--surface-elevated);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-lg);
    padding: 14px 16px;
    flex-shrink: 0;
  }

  .notes-title {
    font-size: 0.875rem;
    font-weight: 600;
    color: var(--text-secondary);
    margin: 0 0 8px 0;
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .notes-text {
    font-size: 0.875rem;
    color: var(--text-secondary);
    margin: 0;
    line-height: var(--leading-relaxed);
    white-space: pre-wrap;
  }

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

  .status-success { background: var(--success-muted); color: var(--success); }
  .status-danger { background: var(--danger-muted); color: var(--danger); }
  .status-muted { background: var(--surface-hover); color: var(--text-muted); }

  .badge {
    background: var(--surface-hover);
    color: var(--text-muted);
    border-radius: var(--radius-full);
    padding: 1px 7px;
    font-size: 0.6875rem;
    font-weight: 600;
  }

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

  .btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .btn-sm {
    padding: 6px 10px;
    font-size: 0.8125rem;
  }

  .btn-secondary { background: var(--surface-hover); color: var(--text-primary); border: 1px solid var(--border-default); }
  .btn-secondary:hover:not(:disabled) { background: var(--surface-pressed); }

  @media (max-width: 900px) {
    .output-layout { grid-template-columns: 1fr; }
    .file-tree {
      width: 100%;
      border-right: none;
      border-bottom: 1px solid var(--border-default);
    }
    .version-history {
      width: 100%;
      border-left: none;
      border-top: 1px solid var(--border-default);
    }
  }
</style>
