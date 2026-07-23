<script>
  /**
   * Chat/Prompt view — main conversational interface.
   *
   * Handles project creation via IntakeFlow, real-time message streaming
   * via SSE, task tree visualization, and message composition.
   */
  import { appState } from '$lib/stores/app.svelte.js';
  import * as sse from '$lib/stores/sse.svelte.js';
  import * as api from '$lib/api.js';
  import { showToast } from '$lib/stores/toast.svelte.js';
  import MessageBubble from '$components/chat/MessageBubble.svelte';
  import IntakeFlow from '$components/chat/IntakeFlow.svelte';
  import ProgressSection from '$components/chat/ProgressSection.svelte';
  import TaskTree from '$components/chat/TaskTree.svelte';
  import HelpPopover from '$lib/components/help/HelpPopover.svelte';
  import HelpTooltip from '$lib/components/help/HelpTooltip.svelte';
  import { ChatModeActions } from '$lib/components/workbench/modes/chat';
  import { ResearchModePanel } from '$lib/components/workbench/modes/research';
  import { WritingModePanel } from '$lib/components/workbench/modes/writing';
  import { CreativeWritingModePanel } from '$lib/components/workbench/modes/creative_writing';
  import ChatWorkspace from '$components/views/chat/ChatWorkspace.svelte';

  let messages = $state([]);
  let tasks = $state([]);
  let project = $state(null);
  let inputText = $state('');
  let sending = $state(false);
  let messagesEl = $state(null);
  let attachments = $state([]);
  let dragging = $state(false);
  let fileInputEl = $state(null);
  let loadError = $state(null);
  let projectLoading = $state(false);
  // FSA-0050: free-form vs task-routing mode toggle.  Surfaced by
  // ChatWorkspace's fsa0050-mode-toggle and sent to the backend on every
  // outgoing message so the server can disable task routing.
  let chatMode = $state('task');
  // FSA-0055: grammar / structured-output controls.  GrammarPanel emits a
  // {grammar, response_format} pair via onGrammarChange and we forward
  // both fields into the outgoing message options.
  let grammarConfig = $state({ grammar: null, response_format: null });
  // FSA-0047: vision image inputs.  modelSupportsVision drives the
  // VisionInput component's enabled state via ChatWorkspace; the captured
  // data URLs accumulate in pendingVisionImages and are forwarded into
  // messageOptions on send.
  let modelSupportsVision = $state(false);
  let pendingVisionImages = $state([]);

  // Block dangerous executables; accept everything else (matches backend)
  const BLOCKED_EXTENSIONS = new Set([
    '.exe', '.dll', '.so', '.dylib', '.sys', '.drv',
    '.msi', '.scr', '.com', '.cmd', '.vbs', '.vbe',
    '.wsf', '.wsh', '.lnk', '.pif', '.reg',
  ]);
  const IMAGE_EXTENSIONS = ['.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.bmp'];
  const MAX_FILE_SIZE = 32 * 1024 * 1024; // 32 MiB (matches Claude's per-file limit)

  let hasProject = $derived(appState.currentProjectId != null);
  let activeBranchId = $derived(project?.active_branch_id ?? project?.branch_id ?? 'main');
  let pinnedContextIds = $derived(
    messages
      .slice(-3)
      .map((msg, idx) => msg.id ?? msg.message_id ?? `${msg.role ?? 'message'}-${msg.timestamp ?? idx}`)
      .filter(Boolean)
  );
  let claimCount = $derived(messages.filter((msg) => /claim|source|citation/i.test(msg.content ?? '')).length);
  let contradictionCount = $derived(messages.filter((msg) => /contradict|conflict/i.test(msg.content ?? '')).length);
  let projectWithTasks = $derived(project ? { ...project, tasks } : project);

  /** Auto-scroll messages to bottom. */
  function scrollToBottom() {
    if (messagesEl) {
      requestAnimationFrame(() => {
        messagesEl.scrollTop = messagesEl.scrollHeight;
      });
    }
  }

  /** Handle SSE events from the project stream. */
  function handleSSEMessage(data) {
    if (data.type === 'message' || data.role) {
      messages = [...messages, data];
      scrollToBottom();
    }
  }

  /**
   * Handle a task_started / task_completed / task_failed event.
   *
   * Backend payload shape (from sse_events.py TaskStartEvent / TaskCompleteEvent /
   * TaskFailedEvent):
   *   { task_id, description?, output_summary?, error?, agent_type?, task_index, total_tasks }
   *
   * We normalise each into a task object keyed by task_id and upsert into the
   * tasks list so ProgressSection / TaskTree can render live progress.
   */
  function handleTaskUpdate(data) {
    // Derive a status flag from which event type was received.
    const status = data.error != null ? 'failed' : data.output_summary != null ? 'completed' : 'running';
    const incoming = {
      id: data.task_id,
      description: data.description ?? data.output_summary ?? data.error ?? '',
      status,
      task_index: data.task_index,
      total_tasks: data.total_tasks,
    };
    const idx = tasks.findIndex((t) => t.id === incoming.id);
    if (idx >= 0) {
      tasks = [...tasks.slice(0, idx), incoming, ...tasks.slice(idx + 1)];
    } else {
      tasks = [...tasks, incoming];
    }
  }

  /**
   * Handle a status event — updates aggregate project state.
   *
   * Backend payload shape (from sse_events.py StatusEvent):
   *   { status, total_tasks }
   *
   * When status is 'complete' or 'done' the project is finished and we surface
   * a toast.  All other status values update the project progress display.
   */
  function handleProjectProgress(data) {
    project = { ...project, ...data };
    // Backend signals completion via a status event with status='complete'|'done'.
    if (data.status === 'complete' || data.status === 'done') {
      handleProjectComplete(data);
    }
  }

  function handleProjectComplete(data) {
    project = { ...project, status: 'complete', progress: 100 };
    showToast('Project completed', 'success');
  }

  /** Subscribe to SSE when project changes. */
  $effect(() => {
    const pid = appState.currentProjectId;
    if (!pid) {
      sse.unsubscribe();
      return;
    }

    // Event names MUST match what the backend emits (sse_handler.py _EVENT_TYPE_MAP).
    // 'task.update', 'project.progress', 'project.complete' were wrong — backend
    // never emitted those.  Correct names: task_started / task_completed /
    // task_failed for individual task lifecycle; status for aggregate project state.
    // Project completion is signalled by a status event with status='complete'|'done',
    // which handleProjectProgress already handles — no separate handler needed.
    sse.subscribe(pid, {
      message: handleSSEMessage,
      task_started: handleTaskUpdate,
      task_completed: handleTaskUpdate,
      task_failed: handleTaskUpdate,
      status: handleProjectProgress,
    });

    // Load existing project data
    loadProject(pid);

    return () => sse.unsubscribe();
  });

  async function loadProject(pid) {
    loadError = null;
    projectLoading = true;
    try {
      const data = await api.getProject(pid);
      if (appState.currentProjectId !== pid) return;
      project = data;
      messages = data?.messages ?? data?.conversation ?? [];
      tasks = data?.tasks ?? [];
    } catch (err) {
      if (appState.currentProjectId !== pid) return;
      project = null;
      messages = [];
      tasks = [];
      loadError = `Failed to load project: ${err.message}`;
    } finally {
      if (appState.currentProjectId === pid) {
        projectLoading = false;
      }
    }
  }

  async function handleCreateProject(config) {
    try {
      const result = await api.createProject(config);
      const projectId = result?.project_id ?? result?.id;
      if (projectId) {
        appState.currentProjectId = projectId;
        showToast('Project created', 'success');
      }
    } catch (err) {
      showToast(`Failed to create project: ${err.message}`, 'error');
    }
  }

  /** Validate and add files to the attachment list. */
  function addFiles(files) {
    for (const file of files) {
      const ext = '.' + file.name.split('.').pop().toLowerCase();
      if (BLOCKED_EXTENSIONS.has(ext)) {
        showToast(`File type ${ext} blocked for security`, 'error');
        continue;
      }
      if (file.size > MAX_FILE_SIZE) {
        showToast(`${file.name} exceeds 32 MiB limit`, 'error');
        continue;
      }
      const isImage = IMAGE_EXTENSIONS.includes(ext) || file.type.startsWith('image/');
      const preview = isImage ? URL.createObjectURL(file) : null;
      const id = crypto.randomUUID();
      attachments = [...attachments, { id, file, preview, name: file.name, isImage }];
    }
  }

  function removeAttachment(id) {
    const item = attachments.find((a) => a.id === id);
    if (item?.preview) URL.revokeObjectURL(item.preview);
    attachments = attachments.filter((a) => a.id !== id);
  }

  /** Handle drag events on the chat area. */
  function handleDragOver(e) {
    e.preventDefault();
    dragging = true;
  }

  function handleDragLeave(e) {
    e.preventDefault();
    dragging = false;
  }

  function handleDrop(e) {
    e.preventDefault();
    dragging = false;
    if (e.dataTransfer?.files?.length) {
      addFiles(e.dataTransfer.files);
    }
  }

  /** Handle paste — capture images from clipboard. */
  function handlePaste(e) {
    const items = e.clipboardData?.items;
    if (!items) return;
    for (const item of items) {
      if (item.type.startsWith('image/')) {
        e.preventDefault();
        const blob = item.getAsFile();
        if (blob) {
          const ext = blob.type.split('/')[1] || 'png';
          const file = new File([blob], `pasted-image.${ext}`, { type: blob.type });
          addFiles([file]);
        }
      }
    }
  }

  function openFilePicker() {
    fileInputEl?.click();
  }

  function handleFileInput(e) {
    if (e.target.files?.length) {
      addFiles(e.target.files);
      e.target.value = '';
    }
  }

  async function sendMessage() {
    const text = inputText.trim();
    if ((!text && attachments.length === 0) || !appState.currentProjectId || sending) return;

    // Capture project id once — async awaits below must not re-read appState mid-send
    const projectId = appState.currentProjectId;
    sending = true;
    const pendingText = text;
    const pendingAttachments = [...attachments];
    inputText = '';
    attachments = [];

    // Optimistically add user message without blob preview URLs — the URLs
    // are revoked in finally and must not survive past this function.
    const optimisticAttachments = pendingAttachments.map((a) => ({
      filename: a.name,
      type: '.' + a.name.split('.').pop().toLowerCase(),
      isImage: a.isImage,
    }));
    const preSendLength = messages.length;
    messages = [...messages, {
      role: 'user',
      content: pendingText,
      timestamp: new Date().toISOString(),
      attachments: optimisticAttachments.length > 0 ? optimisticAttachments : undefined,
    }];
    scrollToBottom();

    try {
      // Upload attachments first
      const uploadedRefs = [];
      for (const att of pendingAttachments) {
        const result = await api.uploadAttachment(projectId, att.file);
        uploadedRefs.push({
          id: result.attachment_id,
          filename: result.filename,
          type: result.type,
          size: result.size,
        });
      }

      const messageOptions = {
        mode: chatMode,
        grammar: grammarConfig.grammar,
        response_format: grammarConfig.response_format,
        images: pendingVisionImages,
      };
      await api.sendMessage(projectId, pendingText, uploadedRefs, messageOptions);
      // Clear vision attachments after a successful send so they don't
      // re-attach to the next message.
      pendingVisionImages = [];
    } catch (err) {
      showToast(`Failed to send: ${err.message}`, 'error');
      // Remove the optimistic message — backend never stored it
      messages = messages.slice(0, preSendLength);
    } finally {
      // Revoke blob preview URLs from the input strip
      for (const att of pendingAttachments) {
        if (att.preview) URL.revokeObjectURL(att.preview);
      }
      sending = false;
    }
  }

  function handleInputKeydown(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  }

  async function cancelProject() {
    if (!appState.currentProjectId) return;
    try {
      await api.cancelProject(appState.currentProjectId);
      showToast('Project cancelled', 'info');
    } catch (err) {
      showToast(`Cancel failed: ${err.message}`, 'error');
    }
  }
</script>

<div class="chat-view">
  {#if !hasProject}
    <!-- No active project — show intake flow -->
    <div class="chat-intake">
      <IntakeFlow oncreate={handleCreateProject} />
    </div>
  {:else}
    <!-- Active project — show chat interface -->
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div
      class="chat-layout"
      ondragover={handleDragOver}
      ondragleave={handleDragLeave}
      ondrop={handleDrop}
    >
      {#if dragging}
        <div class="drop-overlay">
          <div class="drop-overlay-content">
            <i class="fas fa-cloud-upload-alt" aria-hidden="true"></i>
            <p>Drop files here</p>
          </div>
        </div>
      {/if}

      <!-- Messages area -->
      <div class="chat-messages" bind:this={messagesEl} aria-live="polite">
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
            <i class="fas fa-exclamation-triangle" aria-hidden="true"></i>
            {loadError}
          </div>
        {:else if projectLoading}
          <div class="chat-empty" aria-busy="true">
            <i class="fas fa-spinner fa-spin" aria-hidden="true"></i>
            <p>Loading project...</p>
          </div>
        {:else}
          {#each messages as msg ((msg.id ?? (msg.timestamp + ':' + msg.role)))}
            <MessageBubble message={msg} />
          {/each}

          {#if messages.length === 0}
            <div class="chat-empty">
              <i class="fas fa-comments" aria-hidden="true"></i>
              <p>No messages yet.</p>
            </div>
          {/if}
        {/if}
      </div>

      <!-- Task sidebar -->
      {#if tasks.length > 0}
        <div class="chat-sidebar">
          <h3 class="sidebar-title">
            <i class="fas fa-tasks" aria-hidden="true"></i>
            Tasks
          </h3>
          <TaskTree {tasks} />
        </div>
      {/if}

      <!-- Input area -->
      <div class="chat-input-area">
        <!-- Attachment preview strip -->
        {#if attachments.length > 0}
          <div class="attachment-strip">
            {#each attachments as att (att.id)}
              <div class="attachment-preview">
                {#if att.isImage && att.preview}
                  <img src={att.preview} alt={att.name} class="attachment-thumb" />
                {:else}
                  <div class="attachment-file-icon">
                    <i class="fas fa-file-code" aria-hidden="true"></i>
                  </div>
                {/if}
                <span class="attachment-name" title={att.name}>{att.name}</span>
                <button
                  class="attachment-remove"
                  onclick={() => removeAttachment(att.id)}
                  aria-label="Remove {att.name}"
                >
                  <i class="fas fa-times" aria-hidden="true"></i>
                </button>
              </div>
            {/each}
          </div>
        {/if}

        <div class="chat-input-wrap">
          <input
            type="file"
            bind:this={fileInputEl}
            onchange={handleFileInput}
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
            <i class="fas fa-paperclip" aria-hidden="true"></i>
          </button>
          <textarea
            class="textarea chat-textarea"
            bind:value={inputText}
            onkeydown={handleInputKeydown}
            onpaste={handlePaste}
            placeholder="Send a message, paste an image, or drop files..."
            rows="2"
            disabled={sending}
            aria-label="Message input"
          ></textarea>
          <div class="chat-input-actions">
            <button
              class="btn btn-primary"
              onclick={sendMessage}
              disabled={(!inputText.trim() && attachments.length === 0) || sending}
              aria-label="Send message"
            >
              <i class="fas" class:fa-paper-plane={!sending} class:fa-spinner={sending} class:fa-spin={sending} aria-hidden="true"></i>
            </button>
            {#if project?.status === 'in_progress'}
              <button class="btn btn-ghost btn-danger-text" onclick={cancelProject} title="Cancel project">
                <i class="fas fa-stop" aria-hidden="true"></i>
              </button>
            {/if}
          </div>
        </div>
      </div>
    </div>
  {/if}
</div>

<style>
  .chat-view {
    height: 100%;
    display: flex;
    flex-direction: column;
  }

  .chat-intake {
    flex: 1;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 48px 24px;
  }

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
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .chat-sidebar {
    grid-column: 2;
    grid-row: 1 / -1;
    width: 300px;
    border-left: 1px solid var(--border-default);
    padding: 16px;
    overflow-y: auto;
  }

  .sidebar-title {
    font-size: 0.875rem;
    font-weight: 600;
    color: var(--text-primary);
    margin: 0 0 12px 0;
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .sidebar-title i { color: var(--text-muted); }

  .chat-input-area {
    grid-column: 1;
    grid-row: 2;
    padding: 12px 24px 20px;
    border-top: 1px solid var(--border-default);
  }

  .chat-input-wrap {
    display: flex;
    gap: 10px;
    align-items: flex-end;
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
    flex-shrink: 0;
  }

  .chat-empty {
    text-align: center;
    padding: 48px;
    color: var(--text-muted);
    font-size: 0.9375rem;
  }

  .chat-empty i {
    font-size: 2rem;
    margin-bottom: 12px;
    display: block;
    opacity: 0.5;
  }

  .workbench-mode-strip {
    display: grid;
    grid-template-columns: minmax(240px, 1.4fr) repeat(3, minmax(160px, 1fr));
    gap: 8px;
    margin-bottom: 12px;
  }

  .btn-danger-text {
    color: var(--danger);
  }

  .btn-danger-text:hover {
    background: rgba(240, 98, 98, 0.1);
  }

  /* Drag-drop overlay */
  .drop-overlay {
    position: absolute;
    inset: 0;
    z-index: 50;
    background: rgba(78, 154, 249, 0.08);
    border: 2px dashed var(--primary);
    border-radius: 12px;
    display: flex;
    align-items: center;
    justify-content: center;
    pointer-events: none;
  }

  .drop-overlay-content {
    text-align: center;
    color: var(--primary);
    font-size: 1.125rem;
    font-weight: 500;
  }

  .drop-overlay-content i {
    font-size: 2.5rem;
    margin-bottom: 8px;
    display: block;
  }

  .chat-layout {
    position: relative;
  }

  /* Attachment preview strip */
  .attachment-strip {
    display: flex;
    gap: 8px;
    padding: 8px 0;
    overflow-x: auto;
    flex-wrap: wrap;
  }

  .attachment-preview {
    display: flex;
    align-items: center;
    gap: 6px;
    background: var(--surface-raised);
    border: 1px solid var(--border-default);
    border-radius: 8px;
    padding: 4px 8px;
    max-width: 200px;
  }

  .attachment-thumb {
    width: 36px;
    height: 36px;
    object-fit: cover;
    border-radius: 4px;
    flex-shrink: 0;
  }

  .attachment-file-icon {
    width: 36px;
    height: 36px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: rgba(255, 255, 255, 0.05);
    border-radius: 4px;
    color: var(--text-muted);
    flex-shrink: 0;
  }

  .attachment-name {
    font-size: 0.75rem;
    color: var(--text-secondary);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    flex: 1;
    min-width: 0;
  }

  .attachment-remove {
    background: none;
    border: none;
    color: var(--text-muted);
    cursor: pointer;
    padding: 2px;
    font-size: 0.75rem;
    flex-shrink: 0;
    border-radius: 4px;
  }

  .attachment-remove:hover {
    color: var(--danger);
    background: rgba(240, 98, 98, 0.1);
  }

  /* Attach button */
  .btn-attach {
    color: var(--text-muted);
    flex-shrink: 0;
  }

  .btn-attach:hover {
    color: var(--primary);
  }

  @media (max-width: 900px) {
    .chat-sidebar { display: none; }
    .chat-layout { grid-template-columns: 1fr; }
    .workbench-mode-strip { grid-template-columns: 1fr; }
  }
</style>
