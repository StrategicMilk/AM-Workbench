<script>
  /**
   * Chat message bubble with markdown rendering and code highlighting.
   *
   * Renders user and assistant messages with appropriate styling. Assistant
   * messages are parsed as markdown using marked.js and code blocks are
   * syntax-highlighted with highlight.js.
   *
   * @prop {object} message - Message object with role, content, timestamp.
   */
  import { time } from '$lib/utils/format.js';
  import { getAttachmentUrl } from '$lib/api.js';
  import { renderSafeMarkdown } from '$lib/markdown.js';

  let { message } = $props();
  // Includes .webp so history-reloaded attachments render as images on the
  // same path as locally-previewed ones (which set att.isImage on upload).
  const IMAGE_TYPES = ['.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.bmp'];

  let isUser = $derived(message.role === 'user');
  let isThinking = $derived(message.role === 'thinking' || message.type === 'thinking');

  let htmlContent = $derived(isUser ? null : renderSafeMarkdown(message.content ?? ''));

  let thinkingExpanded = $state(false);
</script>

<div
  class="message"
  class:message-user={isUser}
  class:message-assistant={!isUser && !isThinking}
  class:message-thinking={isThinking}
>
  <div class="message-avatar" aria-hidden="true">
    {#if isUser}
      <i class="fas fa-user"></i>
    {:else if isThinking}
      <i class="fas fa-lightbulb"></i>
    {:else}
      <i class="fas fa-brain"></i>
    {/if}
  </div>

  <div class="message-body">
    {#if isThinking}
      <button
        class="thinking-toggle"
        onclick={() => { thinkingExpanded = !thinkingExpanded; }}
        aria-expanded={thinkingExpanded}
      >
        <i class="fas" class:fa-chevron-right={!thinkingExpanded} class:fa-chevron-down={thinkingExpanded}></i>
        <span>Thinking trace</span>
      </button>
      {#if thinkingExpanded}
        <div class="thinking-content">
          {message.content}
        </div>
      {/if}
    {:else if isUser}
      <div class="message-text">{message.content}</div>
    {:else}
      <!-- eslint-disable-next-line svelte/no-at-html-tags -->
      <div class="message-text markdown-body">{@html htmlContent}</div>
    {/if}

    {#if message.attachments?.length}
      <div class="message-attachments">
        {#each message.attachments as att}
          {#if att.isImage || IMAGE_TYPES.includes(att.type)}
            {#if att.preview}
              <!-- Local preview (optimistic, before upload) -->
              <img src={att.preview} alt={att.filename} class="msg-attachment-img" />
            {:else if att.id}
              <!-- Served from backend -->
              <img src={getAttachmentUrl(att.id)} alt={att.filename} class="msg-attachment-img" />
            {/if}
          {:else}
            <a
              class="msg-attachment-file"
              href={att.id ? getAttachmentUrl(att.id) : '#'}
              target="_blank"
              rel="noopener"
            >
              <i class="fas fa-file-code"></i>
              <span>{att.filename}</span>
            </a>
          {/if}
        {/each}
      </div>
    {/if}

    {#if message.timestamp}
      <time class="message-time" datetime={message.timestamp}>
        {time(message.timestamp)}
      </time>
    {/if}
  </div>
</div>

<style>
  .message {
    display: flex;
    gap: 12px;
    padding: 12px 0;
  }

  .message-avatar {
    width: 32px;
    height: 32px;
    border-radius: 8px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 14px;
    flex-shrink: 0;
  }

  .message-user .message-avatar {
    background: rgba(78, 154, 249, 0.15);
    color: var(--primary);
  }

  .message-assistant .message-avatar {
    background: rgba(56, 211, 159, 0.15);
    color: var(--success);
  }

  .message-thinking .message-avatar {
    background: rgba(245, 165, 36, 0.15);
    color: var(--warning);
  }

  .message-body {
    flex: 1;
    min-width: 0;
  }

  .message-text {
    font-size: 0.9375rem;
    line-height: 1.6;
    color: var(--text-primary);
  }

  /* Markdown body styling */
  .message-text :global(pre) {
    background: var(--base-bg);
    border: 1px solid var(--border-default);
    border-radius: 8px;
    padding: 12px 16px;
    overflow-x: auto;
    font-size: 0.8125rem;
    margin: 8px 0;
  }

  .message-text :global(code) {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 0.8125rem;
  }

  .message-text :global(p code) {
    background: rgba(255, 255, 255, 0.06);
    padding: 2px 6px;
    border-radius: 4px;
  }

  .message-text :global(blockquote) {
    border-left: 3px solid var(--primary);
    padding-left: 12px;
    color: var(--text-secondary);
    margin: 8px 0;
  }

  .message-text :global(a) {
    color: var(--primary);
  }

  .message-text :global(table) {
    border-collapse: collapse;
    width: 100%;
    margin: 8px 0;
  }

  .message-text :global(th),
  .message-text :global(td) {
    border: 1px solid var(--border-default);
    padding: 8px 12px;
    text-align: left;
    font-size: 0.8125rem;
  }

  .message-text :global(th) {
    background: rgba(255, 255, 255, 0.04);
    font-weight: 600;
  }

  .message-time {
    display: block;
    font-size: 0.6875rem;
    color: var(--text-muted);
    margin-top: 6px;
  }

  .thinking-toggle {
    display: flex;
    align-items: center;
    gap: 6px;
    background: none;
    border: none;
    color: var(--text-muted);
    cursor: pointer;
    font-family: inherit;
    font-size: 0.8125rem;
    padding: 4px 0;
  }

  .thinking-toggle:hover {
    color: var(--text-secondary);
  }

  .thinking-content {
    margin-top: 8px;
    padding: 12px;
    background: rgba(245, 165, 36, 0.04);
    border-radius: 8px;
    font-size: 0.8125rem;
    color: var(--text-secondary);
    font-family: 'IBM Plex Mono', monospace;
    white-space: pre-wrap;
    line-height: 1.5;
  }

  /* Attachments in messages */
  .message-attachments {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-top: 8px;
  }

  .msg-attachment-img {
    max-width: 400px;
    max-height: 300px;
    border-radius: 8px;
    border: 1px solid var(--border-default);
    cursor: pointer;
    transition: opacity 0.15s;
  }

  .msg-attachment-img:hover {
    opacity: 0.9;
  }

  .msg-attachment-file {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 6px 12px;
    background: var(--surface-raised, rgba(255, 255, 255, 0.04));
    border: 1px solid var(--border-default);
    border-radius: 8px;
    color: var(--text-secondary);
    font-size: 0.8125rem;
    text-decoration: none;
    transition: background 0.15s;
  }

  .msg-attachment-file:hover {
    background: rgba(255, 255, 255, 0.08);
    color: var(--primary);
  }

  .msg-attachment-file i {
    color: var(--text-muted);
  }
</style>
