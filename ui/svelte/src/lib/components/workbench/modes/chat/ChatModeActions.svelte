<script>
  import * as api from '$lib/api.js';

  const {
    projectId = '',
    activeBranchId = 'main',
    pinnedContextIds = [],
  } = $props();

  let convertingKind = $state('');
  let conversionStatus = $state('');

  const conversionKinds = [
    { kind: 'plan', icon: 'fa-list-check', label: 'Plan' },
    { kind: 'eval', icon: 'fa-vial', label: 'Eval' },
    { kind: 'dataset', icon: 'fa-table', label: 'Dataset' },
    { kind: 'prompt', icon: 'fa-terminal', label: 'Prompt' },
    { kind: 'template', icon: 'fa-layer-group', label: 'Template' },
    { kind: 'evidence_notebook', icon: 'fa-book', label: 'Notebook' },
  ];

  async function convert(kind) {
    if (!projectId || convertingKind) return;
    convertingKind = kind;
    conversionStatus = '';
    try {
      await api.convertChatBranchToArtifact(projectId, {
        kind,
        branch_id: activeBranchId,
        pinned_context_ids: pinnedContextIds,
      });
      conversionStatus = `${kind.replace('_', ' ')} conversion queued`;
    } catch (err) {
      conversionStatus = `Conversion blocked: ${err.message}`;
    } finally {
      convertingKind = '';
    }
  }
</script>

<section class="chat-mode-actions" aria-label="Chat mode conversions">
  <div class="mode-header">
    <i class="fas fa-code-branch" aria-hidden="true"></i>
    <span>Chat</span>
    <small>{activeBranchId}</small>
  </div>
  <div class="action-grid">
    {#each conversionKinds as action (action.kind)}
      <button
        type="button"
        class="mode-action"
        title={`Convert chat to ${action.label}`}
        aria-label={`Convert chat to ${action.label}`}
        disabled={!projectId || convertingKind !== ''}
        onclick={() => convert(action.kind)}
      >
        <i class={`fas ${convertingKind === action.kind ? 'fa-spinner fa-spin' : action.icon}`} aria-hidden="true"></i>
        <span>{action.label}</span>
      </button>
    {/each}
  </div>
  {#if conversionStatus}
    <p class="conversion-status" role="status" aria-live="polite">{conversionStatus}</p>
  {/if}
</section>

<style>
  .chat-mode-actions {
    display: grid;
    gap: 8px;
    padding: 10px;
    border: 1px solid var(--border-default);
    border-radius: 6px;
    background: var(--surface-primary);
  }

  .mode-header {
    display: flex;
    align-items: center;
    gap: 8px;
    color: var(--text-primary);
    font-size: 0.8125rem;
    font-weight: 700;
  }

  .mode-header small {
    margin-left: auto;
    color: var(--text-muted);
    font-size: 0.6875rem;
    font-weight: 600;
  }

  .action-grid {
    display: grid;
    grid-template-columns: repeat(3, minmax(72px, 1fr));
    gap: 6px;
  }

  .mode-action {
    min-height: 44px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    border: 1px solid var(--border-default);
    border-radius: 6px;
    background: var(--surface-secondary);
    color: var(--text-primary);
    font: inherit;
    font-size: 0.75rem;
    font-weight: 700;
    cursor: pointer;
  }

  .mode-action:disabled {
    cursor: not-allowed;
    opacity: 0.6;
  }

  .mode-action:not(:disabled):hover,
  .mode-action:focus-visible {
    border-color: var(--primary);
    outline: 2px solid var(--primary-soft, rgba(37, 99, 235, 0.22));
    outline-offset: 2px;
  }

  .conversion-status {
    margin: 0;
    color: var(--text-muted);
    font-size: 0.75rem;
  }
</style>
