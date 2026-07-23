<script>
  import MessageBubble from '$components/chat/MessageBubble.svelte';
  import ProgressSection from '$components/chat/ProgressSection.svelte';
  import TaskTree from '$components/chat/TaskTree.svelte';
  import GrammarPanel from '$components/workbench/GrammarPanel.svelte';
  import VisionInput from '$components/workbench/VisionInput.svelte';
  import { appState } from '$lib/stores/app.svelte.js';
  import Icon from '$lib/a11y/Icon.svelte';
  import { ChatModeActions } from '$lib/components/workbench/modes/chat';
  import { ResearchModePanel } from '$lib/components/workbench/modes/research';
  import { WritingModePanel } from '$lib/components/workbench/modes/writing';
  import { CreativeWritingModePanel } from '$lib/components/workbench/modes/creative_writing';

  let {
    messages = [],
    tasks = [],
    project = null,
    projectWithTasks = null,
    inputText = '',
    sending = false,
    attachments = [],
    visionImages = [],
    chatMode = 'task',
    dragging = false,
    loadError = null,
    projectLoading = false,
    activeBranchId = 'main',
    pinnedContextIds = [],
    claimCount = 0,
    contradictionCount = 0,
    modelSupportsVision = true,
    onDragOver,
    onDragLeave,
    onDrop,
    onRemoveAttachment,
    onFileInput,
    onInputTextChange,
    onInputKeydown,
    onPaste,
    onSendMessage,
    onCancelProject,
    onChatModeChange,
    onGrammarChange,
    onVisionImagesChange,
    onMessagesElementChange,
  } = $props();

  let messagesElement = $state(null);
  let fileInputElement = $state(null);

  $effect(() => {
    onMessagesElementChange(messagesElement);
  });

  function openFilePicker() {
    fileInputElement?.click();
  }
</script>

<div
  class="chat-layout"
  role="region"
  aria-label="Chat workspace with drag-and-drop file upload"
  ondragover={onDragOver}
  ondragleave={onDragLeave}
  ondrop={onDrop}
>
  {#if dragging}
    <div class="drop-overlay">
      <div class="drop-overlay-content">
        <Icon name="cloud-upload-alt" />
        <p>Drop files here</p>
      </div>
    </div>
  {/if}

  <div class="chat-messages" bind:this={messagesElement} aria-live="polite">
    <ProgressSection project={projectWithTasks} />
    <div class="workbench-mode-strip" aria-label="Workbench mode shortcuts">
      <ChatModeActions
        projectId={appState.currentProjectId}
        {activeBranchId}
        {pinnedContextIds}
      />
      <ResearchModePanel
        {claimCount}
        {contradictionCount}
        freshnessStatus={claimCount > 0 ? 'tracked' : 'pending'}
      />
      <WritingModePanel
        audience={project?.audience ?? 'general'}
        draftBranch={activeBranchId}
        verifiedFacts={claimCount}
      />
      <CreativeWritingModePanel
        characterCount={project?.character_count ?? 0}
        sceneBeatCount={project?.scene_beat_count ?? 0}
        voiceScore={project?.voice_conformance_score ?? 0}
      />
    </div>

    {#if loadError}
      <div class="load-error" role="alert">
        <Icon name="exclamation-triangle" />
        {loadError}
      </div>
    {:else if projectLoading}
      <div class="chat-empty" aria-busy="true">
        <Icon name="spinner" class="fa-spin" />
        <p>Loading project...</p>
      </div>
    {:else}
      {#each messages as msg ((msg.id ?? (msg.timestamp + ':' + msg.role)))}
        <MessageBubble message={msg} />
      {/each}

      {#if messages.length === 0}
        <div class="chat-empty">
          <Icon name="comments" />
          <p>No messages yet.</p>
        </div>
      {/if}
    {/if}
  </div>

  {#if tasks.length > 0 && chatMode !== 'free_form'}
    <div class="chat-sidebar">
      <h3 class="sidebar-title">
        <Icon name="tasks" />
        Tasks
      </h3>
      <TaskTree {tasks} />
    </div>
  {/if}

  <div class="chat-input-area">
    <div class="chat-mode-toggle" data-testid="fsa0050-mode-toggle" aria-label="Conversation mode toggle">
      <button
        type="button"
        class:active={chatMode === 'task'}
        aria-pressed={chatMode === 'task'}
        onclick={() => onChatModeChange('task')}
      >
        <Icon name="list-check" />
        Task
      </button>
      <button
        type="button"
        class:active={chatMode === 'free_form'}
        aria-pressed={chatMode === 'free_form'}
        onclick={() => onChatModeChange('free_form')}
      >
        <Icon name="comment" />
        Free-form
      </button>
    </div>

    <GrammarPanel
      disabled={sending}
      onGrammarChange={onGrammarChange}
    />

    {#if attachments.length > 0}
      <div class="attachment-strip">
        {#each attachments as att (att.id)}
          <div class="attachment-preview">
            {#if att.isImage && att.preview}
              <img src={att.preview} alt={att.name} class="attachment-thumb" />
            {:else}
              <div class="attachment-file-icon">
                <Icon name="file-code" />
              </div>
            {/if}
            <span class="attachment-name" title={att.name}>{att.name}</span>
            <button
              class="attachment-remove"
              onclick={() => onRemoveAttachment(att.id)}
              aria-label="Remove {att.name}"
            >
              <Icon name="times" />
            </button>
          </div>
        {/each}
      </div>
    {/if}

    <div class="chat-input-wrap">
      <input
        type="file"
        bind:this={fileInputElement}
        onchange={onFileInput}
        multiple
        hidden
        accept="image/*,.pdf,.txt,.md,.csv,.json"
        aria-hidden="true"
      />
      <button
        class="btn btn-ghost btn-attach"
        onclick={openFilePicker}
        title="Attach files"
        aria-label="Attach files"
      >
        <Icon name="paperclip" />
      </button>
      <VisionInput
        disabled={sending}
        supportsVision={modelSupportsVision}
        images={visionImages}
        onImagesChange={onVisionImagesChange}
      />
      <textarea
        class="textarea chat-textarea"
        value={inputText}
        oninput={(event) => onInputTextChange(event.currentTarget.value)}
        onkeydown={onInputKeydown}
        onpaste={onPaste}
        placeholder="Send a message, paste an image, or drop files..."
        rows="2"
        disabled={sending}
        aria-label="Message input"
      ></textarea>
      <div class="chat-input-actions">
        <button
          class="btn btn-primary"
          onclick={onSendMessage}
          disabled={(!inputText.trim() && attachments.length === 0) || sending}
          aria-label="Send message"
        >
          <Icon name={sending ? 'spinner' : 'paper-plane'} class={sending ? 'fa-spin' : ''} />
        </button>
        {#if project?.status === 'in_progress'}
          <button class="btn btn-ghost btn-danger-text" onclick={onCancelProject} title="Cancel project" aria-label="Cancel project">
            <Icon name="stop" />
          </button>
        {/if}
      </div>
    </div>
  </div>
</div>

<style>
  .chat-layout {
    flex: 1;
    display: grid;
    grid-template-columns: 1fr auto;
    grid-template-rows: 1fr auto;
    min-height: 0;
  }

  .chat-messages {
    grid-column: 1;
    grid-row: 1;
    overflow-y: auto;
    padding: 20px 24px;
    min-height: 0;
  }

  .workbench-mode-strip {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 12px;
    margin-bottom: 16px;
  }

  .chat-empty {
    height: 100%;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 12px;
    color: var(--text-muted);
    font-size: 0.9375rem;
  }

  .chat-empty i {
    font-size: 2rem;
    opacity: 0.35;
  }

  .load-error {
    display: flex;
    align-items: center;
    gap: 10px;
    color: var(--danger);
    background: var(--danger-muted);
    border: 1px solid rgba(240, 98, 98, 0.25);
    border-radius: var(--radius-md);
    padding: 12px 14px;
    font-size: 0.875rem;
  }

  .chat-sidebar {
    grid-column: 2;
    grid-row: 1 / 3;
    width: 280px;
    border-left: 1px solid var(--border-default);
    background: var(--surface-elevated);
    padding: 16px;
    overflow-y: auto;
  }

  .sidebar-title {
    font-size: 0.875rem;
    font-weight: 600;
    color: var(--text-secondary);
    margin: 0 0 12px 0;
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .chat-input-area {
    grid-column: 1;
    grid-row: 2;
    border-top: 1px solid var(--border-default);
    background: var(--surface-elevated);
    padding: 12px 16px;
  }

  .chat-mode-toggle {
    display: inline-flex;
    gap: 4px;
    margin-bottom: 8px;
    padding: 3px;
    border: 1px solid var(--border-default);
    border-radius: var(--radius-full);
    background: var(--surface-bg);
  }

  .chat-mode-toggle button {
    border: none;
    border-radius: var(--radius-full);
    background: transparent;
    color: var(--text-muted);
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font: inherit;
    font-size: 0.8125rem;
    min-height: 44px;
    padding: 6px 12px;
  }

  .chat-mode-toggle button.active {
    background: var(--primary-muted);
    color: var(--primary);
  }

  .attachment-strip {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-bottom: 8px;
  }

  .attachment-preview {
    display: flex;
    align-items: center;
    gap: 6px;
    background: var(--surface-bg);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-md);
    padding: 4px 8px;
    max-width: 240px;
  }

  .attachment-thumb {
    width: 32px;
    height: 32px;
    border-radius: var(--radius-sm);
    object-fit: cover;
  }

  .attachment-file-icon {
    width: 32px;
    height: 32px;
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--text-muted);
  }

  .attachment-name {
    font-size: 0.8125rem;
    color: var(--text-secondary);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .attachment-remove {
    background: none;
    border: none;
    color: var(--text-muted);
    cursor: pointer;
    min-width: 44px;
    min-height: 44px;
    padding: 2px;
  }

  .attachment-remove:hover {
    color: var(--danger);
  }

  .chat-input-wrap {
    display: flex;
    align-items: flex-end;
    gap: 8px;
  }

  .chat-textarea {
    flex: 1;
    resize: none;
    min-height: 44px;
    max-height: 160px;
  }

  .chat-input-actions {
    display: flex;
    gap: 6px;
  }

  .textarea {
    background: var(--surface-bg);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-md);
    color: var(--text-primary);
    font-family: inherit;
    font-size: 0.9375rem;
    padding: 10px 12px;
    box-sizing: border-box;
  }

  .textarea:focus {
    outline: 2px solid transparent;
    outline-offset: 2px;
    border-color: var(--primary);
    box-shadow: 0 0 0 2px var(--primary-muted);
  }

  .btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    padding: 10px 14px;
    border-radius: var(--radius-md);
    font-size: 0.875rem;
    font-weight: 500;
    font-family: inherit;
    cursor: pointer;
    border: none;
    min-height: 44px;
    transition: background var(--transition-base);
  }

  .btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .btn-primary {
    background: var(--primary);
    color: var(--text-on-primary);
  }

  .btn-primary:hover:not(:disabled) { background: var(--primary-hover); }
  .btn-ghost { background: transparent; color: var(--text-muted); }
  .btn-ghost:hover:not(:disabled) { background: var(--surface-hover); color: var(--text-primary); }
  .btn-danger-text { color: var(--danger); }
  .btn-attach { padding: 10px; width: 44px; }

  .drop-overlay {
    position: absolute;
    inset: 0;
    z-index: 10;
    background: rgba(78, 154, 249, 0.12);
    backdrop-filter: blur(2px);
    display: flex;
    align-items: center;
    justify-content: center;
    pointer-events: none;
  }

  .drop-overlay-content {
    background: var(--surface-elevated);
    border: 2px dashed var(--primary);
    border-radius: var(--radius-lg);
    padding: 32px 48px;
    text-align: center;
    color: var(--primary);
  }

  .drop-overlay-content i {
    font-size: 2.5rem;
    margin-bottom: 12px;
  }

  @media (max-width: 900px) {
    .chat-layout { grid-template-columns: 1fr; }
    .chat-sidebar { display: none; }
  }
</style>
