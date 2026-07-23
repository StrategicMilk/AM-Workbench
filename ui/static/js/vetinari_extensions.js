/**
 * Vetinari UI Extensions
 * =======================
 * New and improved functionality that extends/overrides the base app.js.
 * Loaded AFTER app.js so it can override globals.
 *
 * Features added here:
 *   - Project Intake Form submission
 *   - Improved chat message rendering (markdown + streaming)
 *   - Todo task list updates
 *   - Goal verification UI
 *   - Rules configuration
 *   - Image generation UI
 *   - Training UI
 *   - Hardware detection
 *   - Replace window.prompt/alert/confirm with custom modals
 */

'use strict';

// ──────────────────────────────────────────────────────────────────────────────
// Utility: Custom modal dialogs (replaces window.alert/prompt/confirm)
// ──────────────────────────────────────────────────────────────────────────────

class VModalElement extends HTMLElement {
    connectedCallback() {
        // No-op: rendering is handled by show methods
    }

    disconnectedCallback() {
        this._cleanup();
    }

    _cleanup() {
        const backdrop = this.querySelector('.modal-backdrop');
        if (backdrop) backdrop.remove();
    }
}

// Register the custom element
customElements.define('v-modal', VModalElement);

const VModal = {
    _container: null,
    _resolvers: {},
    _focusStack: {},

    _getContainer() {
        if (!this._container) {
            this._container = document.createElement('v-modal');
            document.body.appendChild(this._container);

            // Delegated event handling for all modal interactions
            this._container.addEventListener('click', (e) => {
                const actionEl = e.target.closest('[data-modal-action]');
                if (actionEl) {
                    const action = actionEl.dataset.modalAction;
                    const modalId = actionEl.dataset.modalId;
                    if (action === 'close') {
                        VModal._close(modalId, actionEl.dataset.modalValue === 'true' ? true : actionEl.dataset.modalValue === 'null' ? null : false);
                    } else if (action === 'close-prompt') {
                        VModal._closePrompt(modalId);
                    }
                    return;
                }
                // Click on backdrop (but not modal-content) closes the modal
                if (e.target.classList.contains('modal-backdrop')) {
                    const modalId = e.target.dataset.modalId;
                    if (modalId) VModal._close(modalId, e.target.dataset.modalDefault === 'null' ? null : false);
                }
            });

            this._container.addEventListener('keydown', (e) => {
                // Prompt-specific: Enter submits, Escape cancels
                const input = e.target.closest('[data-modal-prompt-input]');
                if (input) {
                    if (e.key === 'Enter') VModal._closePrompt(input.dataset.modalId);
                    if (e.key === 'Escape') VModal._close(input.dataset.modalId, null);
                    return;
                }

                // Escape closes the topmost open modal
                if (e.key === 'Escape') {
                    const backdrop = e.target.closest('.modal-backdrop');
                    if (backdrop && backdrop.dataset.modalId) {
                        VModal._close(backdrop.dataset.modalId, backdrop.dataset.modalDefault === 'null' ? null : false);
                    }
                    return;
                }

                // Tab key: trap focus within the active modal content
                if (e.key === 'Tab') {
                    const modalContent = e.target.closest('.modal-content');
                    if (!modalContent) return;
                    const focusable = modalContent.querySelectorAll(
                        'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
                    );
                    if (focusable.length === 0) return;
                    const first = focusable[0];
                    const last = focusable[focusable.length - 1];
                    if (e.shiftKey) {
                        if (document.activeElement === first) {
                            e.preventDefault();
                            last.focus();
                        }
                    } else {
                        if (document.activeElement === last) {
                            e.preventDefault();
                            first.focus();
                        }
                    }
                }
            });
        }
        return this._container;
    },

    /**
     * Insert modal HTML, save the previously focused element, and focus
     * the first focusable element inside the new modal.
     *
     * @param {string} id - Modal identifier used for focus-stack key.
     * @param {string} html - Modal HTML string to insert.
     */
    _openModal(id, html) {
        this._focusStack[id] = document.activeElement;
        this._getContainer().insertAdjacentHTML('beforeend', html);
        setTimeout(() => {
            const backdrop = document.getElementById(`${id}_backdrop`);
            if (!backdrop) return;
            const focusable = backdrop.querySelectorAll(
                'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
            );
            if (focusable.length > 0) focusable[0].focus();
        }, 50);
    },

    /**
     * Show a confirmation dialog. Returns a Promise<boolean>.
     */
    confirm(message, title = 'Confirm') {
        const safeEscape = typeof VApp !== 'undefined' ? VApp.escapeHtml : escapeHtml;
        return new Promise((resolve) => {
            const id = 'vmc_' + Date.now();
            const html = `
<div class="modal-backdrop" id="${id}_backdrop" data-modal-id="${id}">
    <div class="modal-content" role="dialog" aria-modal="true" aria-labelledby="${id}_title" style="max-width:420px;">
        <div class="modal-header">
            <h3 id="${id}_title">${safeEscape(title)}</h3>
        </div>
        <div class="modal-body">
            <p>${safeEscape(message)}</p>
        </div>
        <div class="modal-footer">
            <button class="btn btn-secondary" data-modal-action="close" data-modal-id="${id}" data-modal-value="false">Cancel</button>
            <button class="btn btn-danger" data-modal-action="close" data-modal-id="${id}" data-modal-value="true">Confirm</button>
        </div>
    </div>
</div>`;
            this._openModal(id, html);
            this._resolvers[id] = resolve;
        });
    },

    /**
     * Show an alert dialog. Returns a Promise<void>.
     */
    alert(message, title = 'Alert') {
        const safeEscape = typeof VApp !== 'undefined' ? VApp.escapeHtml : escapeHtml;
        return new Promise((resolve) => {
            const id = 'vma_' + Date.now();
            const html = `
<div class="modal-backdrop" id="${id}_backdrop" data-modal-id="${id}">
    <div class="modal-content" role="dialog" aria-modal="true" aria-labelledby="${id}_title" style="max-width:420px;">
        <div class="modal-header">
            <h3 id="${id}_title">${safeEscape(title)}</h3>
        </div>
        <div class="modal-body">
            <p>${safeEscape(message)}</p>
        </div>
        <div class="modal-footer">
            <button class="btn btn-primary" data-modal-action="close" data-modal-id="${id}" data-modal-value="true">OK</button>
        </div>
    </div>
</div>`;
            this._openModal(id, html);
            this._resolvers[id] = resolve;
        });
    },

    /**
     * Show a prompt dialog. Returns a Promise<string|null>.
     */
    prompt(message, defaultValue = '', title = 'Input') {
        const safeEscape = typeof VApp !== 'undefined' ? VApp.escapeHtml : escapeHtml;
        return new Promise((resolve) => {
            const id = 'vmp_' + Date.now();
            const html = `
<div class="modal-backdrop" id="${id}_backdrop" data-modal-id="${id}" data-modal-default="null">
    <div class="modal-content" role="dialog" aria-modal="true" aria-labelledby="${id}_title" style="max-width:460px;">
        <div class="modal-header">
            <h3 id="${id}_title">${safeEscape(title)}</h3>
        </div>
        <div class="modal-body">
            <p>${safeEscape(message)}</p>
            <input type="text" class="input" id="${id}_input" value="${safeEscape(defaultValue)}"
                data-modal-prompt-input data-modal-id="${id}"
                style="width:100%;margin-top:0.5rem;">
        </div>
        <div class="modal-footer">
            <button class="btn btn-secondary" data-modal-action="close" data-modal-id="${id}" data-modal-value="null">Cancel</button>
            <button class="btn btn-primary" data-modal-action="close-prompt" data-modal-id="${id}">OK</button>
        </div>
    </div>
</div>`;
            this._focusStack[id] = document.activeElement;
            this._getContainer().insertAdjacentHTML('beforeend', html);
            this._resolvers[id] = resolve;
            // For prompt, focus the text input directly instead of the first focusable
            setTimeout(() => document.getElementById(`${id}_input`)?.focus(), 50);
        });
    },

    /** Close a modal by id with a value and restore prior focus. */
    _close(id, value) {
        const el = document.getElementById(`${id}_backdrop`);
        if (el) el.remove();
        // Restore focus to the element that was active before the modal opened
        const previousFocus = this._focusStack[id];
        if (previousFocus && typeof previousFocus.focus === 'function') {
            previousFocus.focus();
        }
        delete this._focusStack[id];
        if (this._resolvers[id]) {
            this._resolvers[id](value);
            delete this._resolvers[id];
        }
    },

    _closePrompt(id) {
        const input = document.getElementById(`${id}_input`);
        const val = input ? input.value : null;
        this._close(id, val);
    }
};

// Make VModal global
window.VModal = VModal;

// ──────────────────────────────────────────────────────────────────────────────
// Improved message renderer (uses marked.js if available)
// ──────────────────────────────────────────────────────────────────────────────

function _extensionEscapeHtml(value) {
    if (typeof VApp !== 'undefined' && typeof VApp.escapeHtml === 'function') {
        return VApp.escapeHtml(String(value || ''));
    }
    if (typeof window._escapeMarkdownHtml === 'function') {
        return window._escapeMarkdownHtml(value);
    }
    return String(value || '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function _sanitizeExtensionHtml(html) {
    if (typeof window._sanitizeRenderedMarkdownHtml === 'function') {
        return window._sanitizeRenderedMarkdownHtml(html);
    }
    return String(html || '')
        .replace(/<\s*(script|iframe|object|embed|meta|base|link)\b[\s\S]*?<\s*\/\s*\1\s*>/gi, '')
        .replace(/<\s*(script|iframe|object|embed|meta|base|link)\b[^>]*\/?\s*>/gi, '')
        .replace(/\s+on[a-z0-9_-]+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)/gi, '')
        .replace(/\s+(href|src|xlink:href)\s*=\s*(["'])\s*javascript:[\s\S]*?\2/gi, ' $1="#"')
        .replace(/\s+srcdoc\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)/gi, '')
        .replace(/<\s*\/?\s*(?!(?:p|br|pre|code|strong|em|ul|ol|li|blockquote|a|div|span|button|i|h[1-6]|hr|table|thead|tbody|tr|th|td)\b)[^>]*>/gi, '');
}

function _renderSafeMessageMarkdown(text) {
    if (typeof window.renderMarkdown === 'function') {
        return _sanitizeExtensionHtml(window.renderMarkdown(String(text || '')));
    }
    return _extensionEscapeHtml(text).replace(/\n/g, '<br>');
}

/**
 * Override the existing formatMessageContent with a markdown-aware version.
 */
window.formatMessageContent = function(content) {
    if (!content) return '';

    // Detect raw plan dict/JSON responses and format as a plan summary
    var trimmed = content.trimStart();
    if (trimmed.startsWith("{'plan_id'") || trimmed.startsWith('{"plan_id"')) {
        try {
            var jsonStr = content.replace(/'/g, '"').replace(/True/g, 'true').replace(/False/g, 'false').replace(/None/g, 'null');
            var plan = JSON.parse(jsonStr);
            var html = '**Plan Created**\n\n';
            if (plan.goal) html += plan.goal + '\n\n';
            if (plan.tasks && plan.tasks.length > 0) {
                html += '**' + plan.tasks.length + ' Tasks:**\n';
                plan.tasks.forEach(function(t, i) {
                    html += (i + 1) + '. ' + (t.description || t.id || 'Task') + '\n';
                });
            }
            return _renderSafeMessageMarkdown(html);
        } catch (e) {
            // Fall through to normal formatting if parse fails
        }
    }

    return _renderSafeMessageMarkdown(content);
};

/**
 * Append a message to the chat with proper markdown rendering.
 */
function appendChatMessage(role, content, extra = {}) {
    const messagesEl = document.getElementById('chatMessages');
    if (!messagesEl) return;

    // Hide empty state
    const emptyEl = document.getElementById('chatEmpty');
    if (emptyEl) emptyEl.style.display = 'none';

    const div = document.createElement('div');
    div.className = `chat-message ${role}`;
    if (extra.id) div.id = extra.id;

    const avatarIcon = role === 'user' ? 'fa-user' : 'fa-robot';
    const contentClass = extra.error ? 'error' : (extra.warning ? 'warning' : '');

    // Build agent attribution for assistant messages
    let attributionHtml = '';
    if (role === 'assistant' || role === 'agent') {
        const agentName = extra.agent || 'Vetinari';
        const agentType = (extra.agent || 'vetinari').toLowerCase().replace(/\s+/g, '-');
        const timeStr = extra.timestamp || new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        attributionHtml = `
            <div class="msg-attribution">
                <span class="agent-badge agent-${agentType}">${VApp.escapeHtml(agentName)}</span>
                <span class="msg-time">${VApp.escapeHtml(timeStr)}</span>
            </div>`;
    }

    // Build reasoning trace if present
    let reasoningHtml = '';
    if (extra.reasoning) {
        reasoningHtml = `
            <details class="reasoning-trace">
                <summary><i class="fas fa-brain"></i> Reasoning (click to expand)</summary>
                <div class="trace-content">${formatMessageContent(extra.reasoning)}</div>
            </details>`;
    }

    // Build message actions bar for assistant messages
    let actionsHtml = '';
    if (role === 'assistant' || role === 'agent') {
        actionsHtml = `
            <div class="msg-actions">
                <button class="msg-action-btn" data-action="copyMsg" title="Copy">
                    <i class="fas fa-copy"></i>
                </button>
                <button class="msg-action-btn" data-action="retryMsg" title="Retry">
                    <i class="fas fa-redo"></i>
                </button>
                <button class="msg-action-btn feedback-up" data-action="feedbackUp" title="Good response">
                    <i class="fas fa-thumbs-up"></i>
                </button>
                <button class="msg-action-btn feedback-down" data-action="feedbackDown" title="Poor response">
                    <i class="fas fa-thumbs-down"></i>
                </button>
                <button class="msg-action-btn" data-action="editPrompt" title="Edit prompt">
                    <i class="fas fa-pen"></i>
                </button>
            </div>`;
    }

    div.innerHTML = `
        <div class="chat-message-avatar"><i class="fas ${avatarIcon}"></i></div>
        <div class="chat-message-body">
            ${attributionHtml}
            ${reasoningHtml}
            <div class="chat-message-content markdown-body ${contentClass}">${formatMessageContent(content)}</div>
            ${actionsHtml}
        </div>
    `;

    messagesEl.appendChild(div);
    messagesEl.scrollTop = messagesEl.scrollHeight;

    // Re-run syntax highlighting on new code blocks (skip already-highlighted ones)
    if (typeof hljs !== 'undefined') {
        div.querySelectorAll('pre code:not([data-highlighted])').forEach(el => {
            try { hljs.highlightElement(el); } catch (_) { /* ignore highlight failures */ }
        });
    }
    // Re-run mermaid on diagram blocks
    if (typeof mermaid !== 'undefined') {
        div.querySelectorAll('.language-mermaid').forEach(el => {
            mermaid.run({ nodes: [el] });
        });
    }

    return div;
}
window.appendChatMessage = appendChatMessage;

/**
 * Show typing indicator in chat.
 */
function showTypingIndicator() {
    const messagesEl = document.getElementById('chatMessages');
    if (!messagesEl) return null;
    const emptyEl = document.getElementById('chatEmpty');
    if (emptyEl) emptyEl.style.display = 'none';
    const div = document.createElement('div');
    div.className = 'chat-message assistant';
    div.id = 'typingIndicator';
    div.innerHTML = `
        <div class="chat-message-avatar"><i class="fas fa-robot"></i></div>
        <div class="chat-message-body">
            <div class="chat-message-content">
                <div class="typing-indicator">
                    <div class="typing-dot"></div>
                    <div class="typing-dot"></div>
                    <div class="typing-dot"></div>
                </div>
            </div>
        </div>`;
    messagesEl.appendChild(div);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return div;
}

function hideTypingIndicator() {
    document.getElementById('typingIndicator')?.remove();
}

// ──────────────────────────────────────────────────────────────────────────────
// Task Todo List (OpenCode-inspired)
// ──────────────────────────────────────────────────────────────────────────────

const STATUS_ICONS = {
    pending:     '<i class="fas fa-circle task-status-icon pending" title="Pending"></i>',
    ready:       '<i class="fas fa-circle task-status-icon pending" title="Ready"></i>',
    in_progress: '<i class="fas fa-circle-notch fa-spin task-status-icon in_progress" title="Running"></i>',
    running:     '<i class="fas fa-circle-notch fa-spin task-status-icon in_progress" title="Running"></i>',
    completed:   '<i class="fas fa-check-circle task-status-icon completed" title="Done"></i>',
    failed:      '<i class="fas fa-times-circle task-status-icon failed" title="Failed"></i>',
    cancelled:   '<i class="fas fa-ban task-status-icon cancelled" title="Cancelled"></i>',
    blocked:     '<i class="fas fa-lock task-status-icon cancelled" title="Blocked"></i>',
};

/**
 * Move a contextual panel element into #contextPanelBody and auto-open the panel.
 * Idempotent: does nothing if element is already inside the panel body.
 */
window.moveToContextPanel = moveToContextPanel;
function moveToContextPanel(el) {
    if (!el) return;
    const panelBody = document.getElementById('contextPanelBody');
    const panel = document.getElementById('contextPanel');
    if (!panelBody) return;

    // Move element if not already inside context panel body
    if (el.parentElement !== panelBody) {
        panelBody.appendChild(el);
    }

    // Auto-open the context panel
    if (panel && !panel.classList.contains('open')) {
        panel.classList.add('open');
    }
}

/**
 * Render the task todo list - improved version that matches the new CSS.
 */
window.renderTasks = function(tasks) {
    const container = document.getElementById('tasksList');
    const chatTasksEl = document.getElementById('chatTasks');
    const badge = document.getElementById('tasksCountBadge');

    if (!container) return;

    if (!tasks || tasks.length === 0) {
        if (chatTasksEl) chatTasksEl.style.display = 'none';
        return;
    }

    if (chatTasksEl) chatTasksEl.style.display = 'block';
    if (badge) badge.textContent = tasks.length;

    // Move into context panel if not already there
    moveToContextPanel(chatTasksEl);

    container.innerHTML = tasks.map(task => {
        const status = (task.status || 'pending').toLowerCase().replace(' ', '_');
        const icon = STATUS_ICONS[status] || STATUS_ICONS.pending;
        const agent = task.assigned_agent || task.agent_type || '';
        const model = task.assigned_model || task.assigned_model_id || '';
        const desc = task.description || task.name || 'Task';
        const taskId = task.id || task.task_id || '';

        return `
<div class="task-item ${status}" role="listitem" id="task-item-${VApp.escapeHtml(taskId)}">
    <span class="task-status-icon">${icon}</span>
    <div class="task-info">
        <div class="task-description" title="${VApp.escapeHtml(desc)}">${VApp.escapeHtml(desc)}</div>
        <div class="task-meta">
            ${agent ? `<span class="task-agent-badge">${VApp.escapeHtml(agent)}</span>` : ''}
            ${model ? `<span class="task-agent-badge" style="color:var(--secondary)">${VApp.escapeHtml(model)}</span>` : ''}
        </div>
    </div>
    ${task.output || task.error ? `
    <button class="task-expand" data-action="toggleTaskDetails" data-id="${VApp.escapeHtml(taskId)}" title="Show details">
        <i class="fas fa-chevron-down"></i>
    </button>` : ''}
</div>
${task.output || task.error ? `
<div class="task-details" id="task-details-${VApp.escapeHtml(taskId)}">
    ${task.error ? `<div style="color:var(--danger)"><strong>Error:</strong> ${VApp.escapeHtml(task.error)}</div>` : ''}
    ${task.output ? `<div>${VApp.escapeHtml(String(task.output).slice(0, 300))}...</div>` : ''}
</div>` : ''}`;
    }).join('');
};

function toggleTaskDetails(taskId) {
    const el = document.getElementById(`task-details-${taskId}`);
    if (el) el.classList.toggle('open');
}
window.toggleTaskDetails = toggleTaskDetails;

/**
 * Update a single task's status in the todo list.
 */
function updateTaskStatus(taskId, status, extra = {}) {
    const item = document.getElementById(`task-item-${taskId}`);
    if (!item) return;

    // Update class
    item.className = `task-item ${status.toLowerCase()}`;

    // Update icon
    const iconEl = item.querySelector('.task-status-icon');
    if (iconEl) {
        const normalizedStatus = status.toLowerCase().replace(' ', '_');
        iconEl.outerHTML = STATUS_ICONS[normalizedStatus] || STATUS_ICONS.pending;
    }

    // Update description strikethrough
    const desc = item.querySelector('.task-description');
    if (desc && status === 'completed') {
        desc.style.textDecoration = 'line-through';
        desc.style.color = 'var(--text-muted)';
    }
}
window.updateTaskStatus = updateTaskStatus;

// ──────────────────────────────────────────────────────────────────────────────
// Project Intake Form
// ──────────────────────────────────────────────────────────────────────────────

/**
 * Collect the intake form data into a structured object.
 */
function collectIntakeFormData() {
    const name = document.getElementById('intakeProjectName')?.value.trim() || '';
    const goal = document.getElementById('intakeGoal')?.value.trim() || '';
    const details = document.getElementById('intakeDetails')?.value.trim() || '';
    const featuresRaw = document.getElementById('intakeFeatures')?.value || '';
    const avoidRaw = document.getElementById('intakeAvoid')?.value || '';
    const tech = document.getElementById('intakeTech')?.value.trim() || '';
    const priority = document.getElementById('intakePriority')?.value || 'quality';
    const model = document.getElementById('intakeModelSelect')?.value || '';

    // Parse features/avoid as lists
    const features = featuresRaw.split('\n')
        .map(l => l.replace(/^[-*•]\s*/, '').trim())
        .filter(Boolean);
    const avoid = avoidRaw.split('\n')
        .map(l => l.replace(/^[-*•]\s*/, '').trim())
        .filter(Boolean);

    // Collect checkboxes
    const platforms = Array.from(
        document.querySelectorAll('#intakePlatform input:checked')
    ).map(cb => cb.value);
    const expected = Array.from(
        document.querySelectorAll('#intakeExpected input:checked')
    ).map(cb => cb.value);

    // Build combined goal string
    let fullGoal = goal;
    if (details) fullGoal += '\n\nDetails: ' + details;
    if (tech) fullGoal += '\nTechnologies: ' + tech;
    if (platforms.length) fullGoal += '\nTarget platform: ' + platforms.join(', ');
    if (priority) fullGoal += '\nPriority: ' + priority;

    return {
        project_name: name || goal.slice(0, 50),
        goal: fullGoal,
        raw_goal: goal,
        details,
        required_features: features,
        things_to_avoid: avoid,
        tech_stack: tech,
        platforms,
        priority,
        expected_outputs: expected,
        model,
        system_prompt: document.getElementById('systemPromptInput')?.value || '',
        project_rules: document.getElementById('projectRulesInput')?.value || '',
    };
}

/**
 * Clear the intake form.
 */
function clearIntakeForm() {
    ['intakeProjectName', 'intakeGoal', 'intakeDetails', 'intakeFeatures',
     'intakeAvoid', 'intakeTech'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.value = '';
    });
    document.querySelectorAll('#intakePlatform input, #intakeExpected input')
        .forEach(cb => cb.checked = false);
    // Re-check defaults
    ['code', 'tests'].forEach(val => {
        const cb = document.querySelector(`#intakeExpected input[value="${val}"]`);
        if (cb) cb.checked = true;
    });
}
window.clearIntakeForm = clearIntakeForm;

/**
 * Hide the progress section and reset the spinner text.
 */
function hideProgressSection() {
    const ps = document.getElementById('progressSection');
    if (ps) ps.style.display = 'none';
    const wt = document.getElementById('workingOnText');
    if (wt) wt.innerHTML = '';
}
window.hideProgressSection = hideProgressSection;

/**
 * Submit the intake form and create a new project.
 */
async function submitIntakeForm() {
    const submitBtn = document.getElementById('intakeSubmitBtn');
    const formData = collectIntakeFormData();

    if (!formData.raw_goal) {
        VApp.showStatusBanner('Please enter a goal for your project.', 'warning');
        document.getElementById('intakeGoal')?.focus();
        return;
    }

    // Show chat area, hide form and intake flow
    const form = document.getElementById('projectIntakeForm');
    const chatInputArea = document.getElementById('chatInputArea');
    const messagesEl = document.getElementById('chatMessages');
    const projectTitle = document.getElementById('chatProjectTitle');

    if (form) form.style.display = 'none';
    if (window.IntakeFlow) window.IntakeFlow.hide();
    if (projectTitle) projectTitle.textContent = formData.project_name;
    // Update chat input placeholder for follow-up mode
    const chatInputEl = document.getElementById('chatInput');
    if (chatInputEl) chatInputEl.placeholder = 'Ask a follow-up...';

    // Show user's goal as first message
    appendChatMessage('user', formData.raw_goal);
    const typingDiv = showTypingIndicator();

    if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Creating...';
    }

    // Show progress section
    const progressSection = document.getElementById('progressSection');
    if (progressSection) progressSection.style.display = 'block';
    updateProgressBar(0);
    const workingOnText = document.getElementById('workingOnText');
    if (workingOnText) workingOnText.innerHTML = '<i class="fas fa-cog fa-spin"></i> Analyzing goal and planning tasks...';

    try {
        const res = await fetch('/api/new-project', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
            body: JSON.stringify({
                goal: formData.goal,
                project_name: formData.project_name,
                model: formData.model,
                system_prompt: formData.system_prompt,
                project_rules: formData.project_rules,
                required_features: formData.required_features,
                things_to_avoid: formData.things_to_avoid,
                expected_outputs: formData.expected_outputs,
                tech_stack: formData.tech_stack,
                platforms: formData.platforms,
                priority: formData.priority,
                auto_run: true,
            })
        });
        const data = await VApp.safeJsonParse(res);

        hideTypingIndicator();

        if (!data) {
            appendChatMessage('assistant', 'No response from server. Please check your local inference setup.', { error: true });
            hideProgressSection();
            return;
        }

        if (data.error) {
            appendChatMessage('assistant', `Error: ${data.error}`, { error: true });
            addActivity('Error creating project: ' + data.error, 'error');
            hideProgressSection();
            return;
        }

        if (data.needs_context) {
            appendChatMessage('assistant', data.follow_up_question || 'Could you provide more details?');
            VApp.showStatusBanner(data.follow_up_question || 'More details needed', 'info');
            hideProgressSection();
            return;
        }

        if (data.project_id) {
            VApp.currentProjectId = data.project_id;

            // Render conversation
            if (data.conversation && data.conversation.length) {
                renderConversation(data.conversation);
            } else if (data.initial_response) {
                appendChatMessage('assistant', data.initial_response);
            } else {
                appendChatMessage('assistant', `Project created! Planning ${data.tasks?.length || 0} tasks...`);
            }

            // Render tasks todo list
            if (data.tasks && data.tasks.length) {
                renderTasks(data.tasks);
            }

            // Save required_features to project for later verification
            window._currentProjectSpec = {
                goal: formData.raw_goal,
                required_features: formData.required_features,
                things_to_avoid: formData.things_to_avoid,
                expected_outputs: formData.expected_outputs,
            };

            // Subscribe to SSE stream for real-time updates
            subscribeToProjectStream(data.project_id);

            VApp.showStatusBanner(`Project created: ${formData.project_name}`, 'success');
            addActivity(`New project: ${formData.project_name} (${data.tasks?.length || 0} tasks)`);

            // Refresh sidebar
            loadSidebarProjects();
        }

    } catch (err) {
        hideTypingIndicator();
        console.error('Intake form submit error:', err);
        appendChatMessage('assistant', `Failed to create project: ${err.message}`, { error: true });
        addActivity('Error: ' + err.message, 'error');
        hideProgressSection();
    } finally {
        if (submitBtn) {
            submitBtn.disabled = false;
            submitBtn.innerHTML = '<i class="fas fa-rocket"></i> Launch Project';
        }
    }
}
window.submitIntakeForm = submitIntakeForm;

// Show intake form when "New Project" is clicked
function showNewProjectForm() {
    VApp.currentProjectId = null;

    const form = document.getElementById('projectIntakeForm');
    const chatInputArea = document.getElementById('chatInputArea');
    const messagesEl = document.getElementById('chatMessages');
    const chatTasks = document.getElementById('chatTasks');
    const progressSection = document.getElementById('progressSection');
    const finalDelivery = document.getElementById('finalDeliveryPanel');
    const modelRanking = document.getElementById('modelRankingPanel');
    const chatProjectTitle = document.getElementById('chatProjectTitle');

    if (form) form.style.display = 'none';
    if (chatTasks) chatTasks.style.display = 'none';
    if (progressSection) progressSection.style.display = 'none';
    if (finalDelivery) finalDelivery.style.display = 'none';
    if (modelRanking) modelRanking.style.display = 'none';
    if (chatProjectTitle) chatProjectTitle.textContent = 'New Project';
    // Show intake flow category grid instead of old form
    if (window.IntakeFlow) window.IntakeFlow.show();
    // Reset chat input placeholder
    const chatInputReset = document.getElementById('chatInput');
    if (chatInputReset) chatInputReset.placeholder = 'Describe your goal...';

    if (messagesEl) {
        messagesEl.innerHTML = '<div class="chat-empty" id="chatEmpty"><i class="fas fa-comments"></i><p>Choose a category above, or just type your goal below</p></div>';
    }

    // Switch to prompt view
    VApp.switchView('prompt');
}
window.showNewProjectForm = showNewProjectForm;

// ──────────────────────────────────────────────────────────────────────────────
// SSE enhanced handler — updates task list in real-time
// ──────────────────────────────────────────────────────────────────────────────

// Override the existing SSE handler to update the todo list
const _originalSubscribe = window.subscribeToProjectStream;
window.subscribeToProjectStream = function(projectId) {
    // Call original subscription if it exists
    if (typeof _originalSubscribe === 'function') {
        _originalSubscribe(projectId);
    }

    // Also set up our enhanced task-status updater
    if (!projectId) return;

    // Listen for SSE events already being processed by app.js
    // and additionally update our new task list
    document.addEventListener(`sse:task_started:${projectId}`, (e) => {
        updateTaskStatus(e.detail.task_id, 'in_progress');
        const workingOnText = document.getElementById('workingOnText');
        if (workingOnText) {
            workingOnText.innerHTML = `<i class="fas fa-cog fa-spin"></i> Working on: ${VApp.escapeHtml(e.detail.description || e.detail.task_id)}`;
        }
    });

    document.addEventListener(`sse:task_completed:${projectId}`, (e) => {
        updateTaskStatus(e.detail.task_id, 'completed');
    });

    document.addEventListener(`sse:task_failed:${projectId}`, (e) => {
        updateTaskStatus(e.detail.task_id, 'failed');
    });

    document.addEventListener(`sse:task_cancelled:${projectId}`, (e) => {
        updateTaskStatus(e.detail.task_id, 'cancelled');
    });
};

// ──────────────────────────────────────────────────────────────────────────────
// Goal Verification UI
// ──────────────────────────────────────────────────────────────────────────────

async function runGoalVerification() {
    if (!VApp.currentProjectId) return;

    const spec = window._currentProjectSpec || {};
    const finalContent = document.getElementById('finalDeliveryContent')?.innerText || '';

    VApp.showStatusBanner('Verifying deliverable against goal...', 'info');

    try {
        const res = await fetch(`/api/v1/project/${VApp.currentProjectId}/verify-goal`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
            body: JSON.stringify({
                goal: spec.goal || '',
                final_output: finalContent,
                required_features: spec.required_features || [],
                things_to_avoid: spec.things_to_avoid || [],
                expected_outputs: spec.expected_outputs || [],
            })
        });
        const data = await VApp.safeJsonParse(res);

        if (!data || data.error) {
            VApp.showStatusBanner('Verification failed: ' + (data?.error || 'Unknown error'), 'error');
            return;
        }

        renderGoalVerification(data.report);

        // If corrective tasks needed, offer to re-execute
        if (!data.report.fully_compliant && data.corrective_tasks?.length) {
            appendChatMessage('assistant',
                `**Verification found ${data.corrective_tasks.length} gaps:**\n` +
                data.corrective_tasks.map(t => `- ${t.description}`).join('\n') +
                '\n\nShould I fix these issues? (yes/no)',
                { timestamp: new Date().toLocaleTimeString() }
            );
        } else if (data.report.fully_compliant) {
            VApp.showStatusBanner('Deliverable fully meets the project goal!', 'success');
        }
    } catch (err) {
        console.error('Goal verification error:', err);
        VApp.showStatusBanner('Verification error: ' + err.message, 'error');
    }
}
window.runGoalVerification = runGoalVerification;

function renderGoalVerification(report) {
    const verificationEl = document.getElementById('goalVerification');
    const matrixEl = document.getElementById('verificationMatrix');
    const finalPanel = document.getElementById('finalDeliveryPanel');

    if (!verificationEl || !matrixEl) return;

    verificationEl.style.display = 'block';
    if (finalPanel) finalPanel.style.display = 'block';

    const scorePercent = Math.round((report.compliance_score || 0) * 100);
    const scoreColor = scorePercent >= 80 ? 'var(--success)' : scorePercent >= 60 ? 'var(--warning)' : 'var(--danger)';

    matrixEl.innerHTML = `
        <div style="display:flex;align-items:center;gap:1rem;margin-bottom:0.75rem;padding:0.5rem;background:var(--surface-bg);border-radius:var(--radius-md);">
            <span style="font-size:1.5rem;font-weight:700;color:${scoreColor}">${scorePercent}%</span>
            <span style="color:var(--text-secondary);font-size:0.875rem;">Compliance Score</span>
            <span class="badge ${report.fully_compliant ? 'badge-success' : 'badge-warning'}" style="margin-left:auto;">
                ${report.fully_compliant ? '✓ Compliant' : '⚠ Gaps Found'}
            </span>
        </div>
        ${(report.features || []).map(f => `
        <div class="verification-row">
            <span class="verification-status">${f.implemented ? '✅' : '❌'}</span>
            <span class="verification-feature">${VApp.escapeHtml(f.feature)}</span>
            <span class="verification-evidence">${VApp.escapeHtml(f.evidence || '')}</span>
        </div>`).join('')}
        ${!report.security_passed ? `
        <div class="verification-row" style="border-color:var(--danger);">
            <span class="verification-status">🔒</span>
            <span class="verification-feature">Security: ${report.security_findings?.length || 0} findings</span>
            <span class="verification-evidence" style="color:var(--danger);">Failed security audit</span>
        </div>` : ''}
        ${!report.tests_present ? `
        <div class="verification-row">
            <span class="verification-status">⚠</span>
            <span class="verification-feature">Tests</span>
            <span class="verification-evidence">No tests detected</span>
        </div>` : ''}
    `;
}

function pauseCurrentProject() {
    if (!VApp.currentProjectId) return;
    fetch('/api/project/' + VApp.currentProjectId + '/pause', { method: 'POST', headers: { 'X-Requested-With': 'XMLHttpRequest' } })
        .then(function(r) { if (!r.ok) throw new Error('Server returned ' + r.status); return r.json(); })
        .then(function(data) {
            VApp.showStatusBanner('Project paused', 'info');
            var pauseBtn = document.getElementById('pauseProjectBtn');
            var resumeBtn = document.getElementById('resumeProjectBtn');
            if (pauseBtn) pauseBtn.style.display = 'none';
            if (resumeBtn) resumeBtn.style.display = '';
        })
        .catch(function(err) { VApp.showStatusBanner('Pause failed: ' + err.message, 'error'); });
}
window.pauseCurrentProject = pauseCurrentProject;

function resumeCurrentProject() {
    if (!VApp.currentProjectId) return;
    fetch('/api/project/' + VApp.currentProjectId + '/resume', { method: 'POST', headers: { 'X-Requested-With': 'XMLHttpRequest' } })
        .then(function(r) { if (!r.ok) throw new Error('Server returned ' + r.status); return r.json(); })
        .then(function(data) {
            VApp.showStatusBanner('Project resumed', 'success');
            var pauseBtn = document.getElementById('pauseProjectBtn');
            var resumeBtn = document.getElementById('resumeProjectBtn');
            if (pauseBtn) pauseBtn.style.display = '';
            if (resumeBtn) resumeBtn.style.display = 'none';
        })
        .catch(function(err) { VApp.showStatusBanner('Resume failed: ' + err.message, 'error'); });
}
window.resumeCurrentProject = resumeCurrentProject;

function rerunTask(projectId, taskId) {
    fetch('/api/project/' + projectId + '/task/' + taskId + '/rerun', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
        body: JSON.stringify({})
    })
        .then(function(r) { if (!r.ok) throw new Error('Server returned ' + r.status); return r.json(); })
        .then(function(data) {
            if (data.ok) {
                VApp.showStatusBanner('Task re-run started', 'success');
                appendChatMessage('assistant', 'Task ' + taskId + ' has been queued for re-execution.');
            } else {
                VApp.showStatusBanner('Rerun failed: ' + (data.error || 'Unknown error'), 'error');
            }
        })
        .catch(function(err) { VApp.showStatusBanner('Rerun failed: ' + err.message, 'error'); });
}
window.rerunTask = rerunTask;

function approvePlan(planId) {
    if (!planId) return;
    var projectId = planId;
    fetch('/api/project/' + encodeURIComponent(projectId) + '/execute', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
        body: JSON.stringify({ auto_run: true })
    })
        .then(function(r) { if (!r.ok) throw new Error('Server returned ' + r.status); return r.json(); })
        .then(function(data) {
            VApp.showStatusBanner('Plan approved — execution started', 'success');
            appendChatMessage('assistant', 'Plan approved. Execution started for project ' + projectId + '.');
            // Update the plan buttons to show running state
            var btns = document.querySelectorAll('.plan-approve-btn[data-plan-id="' + planId + '"]');
            btns.forEach(function(b) { b.disabled = true; b.textContent = 'Running...'; });
            var rejectBtns = document.querySelectorAll('.plan-reject-btn[data-plan-id="' + planId + '"]');
            rejectBtns.forEach(function(b) { b.style.display = 'none'; });
            // Update status badge
            var summaries = document.querySelectorAll('.plan-summary[data-plan-id="' + planId + '"] .plan-status');
            summaries.forEach(function(s) { s.textContent = 'RUNNING'; s.className = 'plan-status plan-status-running'; });
            // Switch to project view
            if (typeof VApp.loadProject === 'function') {
                VApp.loadProject(projectId);
            }
        })
        .catch(function(err) {
            VApp.showStatusBanner('Failed to start plan: ' + err.message, 'error');
        });
}
window.approvePlan = approvePlan;

function promptPlanFeedback() {
    const safeEscape = typeof VApp !== 'undefined' ? VApp.escapeHtml : escapeHtml;
    return new Promise((resolve) => {
        const id = 'vmpf_' + Date.now();
        const html = `
<div class="modal-backdrop" id="${id}_backdrop" data-modal-id="${id}" data-modal-default="null">
    <div class="modal-content" role="dialog" aria-modal="true" aria-labelledby="${id}_title" style="max-width:520px;">
        <div class="modal-header">
            <h3 id="${id}_title">Reject Plan</h3>
        </div>
        <div class="modal-body">
            <label for="${id}_reason" style="display:block;margin-bottom:0.4rem;">Reason</label>
            <select class="input" id="${id}_reason" style="width:100%;margin-bottom:0.75rem;">
                <option value="OUT_OF_SCOPE">Out of scope</option>
                <option value="TOO_MANY_TASKS">Too many tasks</option>
                <option value="MISSING_DEPENDENCY">Missing dependency</option>
                <option value="WRONG_AGENT">Wrong agent</option>
                <option value="UNSAFE">Unsafe</option>
                <option value="">Free-form note only</option>
            </select>
            <label for="${id}_note" style="display:block;margin-bottom:0.4rem;">Note</label>
            <textarea class="input" id="${id}_note" rows="4" style="width:100%;">${safeEscape('')}</textarea>
        </div>
        <div class="modal-footer">
            <button class="btn btn-secondary" id="${id}_cancel">Cancel</button>
            <button class="btn btn-danger" id="${id}_submit">Reject</button>
        </div>
    </div>
</div>`;
        VModal._focusStack[id] = document.activeElement;
        VModal._getContainer().insertAdjacentHTML('beforeend', html);

        function close(value) {
            const el = document.getElementById(`${id}_backdrop`);
            if (el) el.remove();
            const previousFocus = VModal._focusStack[id];
            if (previousFocus && typeof previousFocus.focus === 'function') previousFocus.focus();
            delete VModal._focusStack[id];
            resolve(value);
        }

        document.getElementById(`${id}_cancel`)?.addEventListener('click', function() { close(null); });
        document.getElementById(`${id}_submit`)?.addEventListener('click', function() {
            close({
                reason_code: document.getElementById(`${id}_reason`)?.value || null,
                free_text: document.getElementById(`${id}_note`)?.value || ''
            });
        });
        setTimeout(() => document.getElementById(`${id}_reason`)?.focus(), 50);
    });
}

function rejectPlan(planId) {
    promptPlanFeedback()
        .then(function(feedback) {
            if (feedback === null) return;
            var reasonCode = feedback.reason_code || null;
            var freeText = (feedback.free_text || '').trim();
            if (!reasonCode && !freeText) return;
            fetch('/api/v1/projects/' + encodeURIComponent(planId) + '/plan-feedback', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
                body: JSON.stringify({
                    plan_id: planId,
                    decision: 'REFUSE',
                    reason_code: reasonCode,
                    severity: 'medium',
                    free_text: freeText
                })
            })
                .then(function(r) { if (!r.ok) throw new Error('Server returned ' + r.status); return r.json(); })
                .then(function() {
                    appendChatMessage('user', 'Plan rejected - reason: ' + (reasonCode || 'free-form note') + (freeText ? ': ' + freeText : ''));
                    // Chat-text free-form notes remain available through requestChanges().
                    var btns = document.querySelectorAll('.plan-approve-btn[data-plan-id="' + planId + '"]');
                    btns.forEach(function(b) { b.style.display = 'none'; });
                    var rejectBtns = document.querySelectorAll('.plan-reject-btn[data-plan-id="' + planId + '"]');
                    rejectBtns.forEach(function(b) { b.disabled = true; b.textContent = 'Rejected'; });
                    var summaries = document.querySelectorAll('.plan-summary[data-plan-id="' + planId + '"] .plan-status');
                    summaries.forEach(function(s) { s.textContent = 'REJECTED'; s.className = 'plan-status plan-status-rejected'; });
                    VApp.showStatusBanner('Plan rejected — feedback sent for revision', 'info');
                })
                .catch(function(err) {
                    VApp.showStatusBanner('Failed to send feedback: ' + err.message, 'error');
                });
        });
}
window.rejectPlan = rejectPlan;

function approveDeliverable() {
    if (!VApp.currentProjectId) return;
    fetch(`/api/project/${VApp.currentProjectId}/approve`, {
        method: 'POST',
        headers: { 'X-Requested-With': 'XMLHttpRequest' }
    })
        .then(r => { if (!r.ok) throw new Error('Server returned ' + r.status); return r.json(); })
        .then(() => {
            VApp.showStatusBanner('Deliverable approved!', 'success');
            appendChatMessage('assistant', '✅ Deliverable approved and saved. Great work!');
        })
        .catch(err => VApp.showStatusBanner('Approval failed: ' + err.message, 'error'));
}
window.approveDeliverable = approveDeliverable;

function requestChanges() {
    VModal.prompt('What changes would you like to make?', '', 'Request Changes')
        .then(changes => {
            if (!changes) return;
            const input = document.getElementById('chatInput');
            if (input) {
                input.value = changes;
                // Trigger send
                document.getElementById('sendMessageBtn')?.click();
            }
        });
}
window.requestChanges = requestChanges;

function copyDeliverable() {
    const content = document.getElementById('finalDeliveryContent')?.innerText || '';
    navigator.clipboard.writeText(content).then(() => {
        VApp.showStatusBanner('Deliverable copied to clipboard', 'success');
    });
}
window.copyDeliverable = copyDeliverable;

// ──────────────────────────────────────────────────────────────────────────────
// Rules Configuration
// ──────────────────────────────────────────────────────────────────────────────

async function loadGlobalRules() {
    try {
        const res = await fetch('/api/v1/rules/global');
        if (!res.ok) {
            VApp.showStatusBanner(`Failed to load global rules (HTTP ${res.status})`, 'error');
            return;
        }
        const data = await res.json();
        if (data.error) {
            VApp.showStatusBanner('Failed to load global rules: ' + data.error, 'error');
            return;
        }
        const ruleItems = Array.isArray(data.rules) ? data.rules : [];
        const rules = ruleItems.map(r => (typeof r === 'string' ? r : (r.content || r.text || JSON.stringify(r)))).join('\n');
        const input = document.getElementById('projectRulesInput');
        if (!input) {
            VApp.showStatusBanner('Rules input not found', 'error');
            return;
        }
        if (rules) {
            input.value = rules;
            VApp.showStatusBanner(`Loaded ${ruleItems.length} global rule(s)`, 'success');
        } else {
            VApp.showStatusBanner('No global rules configured yet', 'info');
        }
    } catch (err) {
        console.error('Failed to load global rules:', err);
        VApp.showStatusBanner('Failed to load global rules: ' + err.message, 'error');
    }
}
window.loadGlobalRules = loadGlobalRules;

async function saveProjectRules() {
    try {
        if (!VApp.currentProjectId) {
            VApp.showStatusBanner('Select a project first', 'warning');
            return;
        }
        const rulesText = document.getElementById('projectRulesInput')?.value || '';
        const rules = rulesText.split('\n').map(r => r.trim()).filter(Boolean);
        const res = await fetch(`/api/v1/rules/project/${VApp.currentProjectId}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
            body: JSON.stringify({ rules }),
        });
        if (!res.ok) throw new Error('Server returned ' + res.status);
        VApp.showStatusBanner('Project rules saved', 'success');
    } catch (err) {
        if (window.VToast) VToast.error('Failed to save rules');
    }
}
window.saveProjectRules = saveProjectRules;

async function saveGlobalRules() {
    try {
        const rulesText = document.getElementById('globalRulesInput')?.value || '';
        const rules = rulesText.split('\n').map(r => r.trim()).filter(Boolean);
        const gRes = await fetch('/api/v1/rules/global', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
            body: JSON.stringify({ rules }),
        });
        if (!gRes.ok) throw new Error('Server returned ' + gRes.status);
        VApp.showStatusBanner('Global rules saved', 'success');
    } catch (err) {
        if (window.VToast) VToast.error('Failed to save rules');
    }
}
window.saveGlobalRules = saveGlobalRules;

async function saveGlobalSystemPrompt() {
    try {
        const prompt = document.getElementById('globalSystemPromptInput')?.value || '';
        const pRes = await fetch('/api/v1/rules/global-prompt', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
            body: JSON.stringify({ prompt }),
        });
        if (!pRes.ok) throw new Error('Server returned ' + pRes.status);
        VApp.showStatusBanner('Global system prompt saved', 'success');
    } catch (err) {
        if (window.VToast) VToast.error('Failed to save rules');
    }
}
window.saveGlobalSystemPrompt = saveGlobalSystemPrompt;

async function loadModelRules() {
    const modelId = document.getElementById('modelRulesModelSelect')?.value;
    if (!modelId) return;
    try {
        const res = await fetch(`/api/v1/rules/model/${encodeURIComponent(modelId)}`);
        if (!res.ok) { console.warn('API error:', res.status); return; }
        const data = await res.json();
        const input = document.getElementById('modelRulesInput');
        const modelRuleItems = Array.isArray(data.rules) ? data.rules : [];
        if (input) input.value = modelRuleItems.map(r => (typeof r === 'string' ? r : (r.content || r.text || JSON.stringify(r)))).join('\n');
    } catch (err) {
        VApp.showStatusBanner('Failed to load model rules: ' + err.message, 'error');
    }
}
window.loadModelRules = loadModelRules;

async function saveModelRules() {
    try {
        const modelId = document.getElementById('modelRulesModelSelect')?.value;
        if (!modelId) { VApp.showStatusBanner('Select a model first', 'warning'); return; }
        const rulesText = document.getElementById('modelRulesInput')?.value || '';
        const rules = rulesText.split('\n').map(r => r.trim()).filter(Boolean);
        await fetch(`/api/v1/rules/model/${encodeURIComponent(modelId)}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
            body: JSON.stringify({ rules }),
        });
        VApp.showStatusBanner('Model rules saved', 'success');
    } catch (err) {
        if (window.VToast) VToast.error('Failed to save rules');
    }
}
window.saveModelRules = saveModelRules;

// ──────────────────────────────────────────────────────────────────────────────
// Hardware Detection
// ──────────────────────────────────────────────────────────────────────────────

async function detectHardware() {
    try {
        const res = await fetch('/api/v1/status');
        if (!res.ok) { console.warn('API error:', res.status); return; }
        const data = await res.json();

        const gpu = data.gpu_name || data.hardware?.gpu || 'Unknown GPU';
        const vram = data.vram_total_gb ? `${data.vram_total_gb} GB` : (data.hardware?.vram || '--');
        const ram = data.ram_gb ? `${data.ram_gb} GB` : (data.hardware?.ram || '--');

        document.getElementById('hwGpu').textContent = gpu;
        document.getElementById('hwVram').textContent = vram;
        document.getElementById('hwRam').textContent = ram;
    } catch (err) {
        document.getElementById('hwGpu').textContent = 'Not detected';
    }
}

// ──────────────────────────────────────────────────────────────────────────────
// Training UI
// ──────────────────────────────────────────────────────────────────────────────

// Training functions are in app.js and exported via VApp.
// See: startTraining, exportTrainingData, loadTrainingStats in app.js

// ──────────────────────────────────────────────────────────────────────────────
// Image Generation UI
// ──────────────────────────────────────────────────────────────────────────────

async function testSdConnection() {
    // Tests the server's configured Stable Diffusion endpoint (not the user-entered host).
    // The user-entered host is saved locally only; the server manages its own SD connection.
    const statusEl = document.getElementById('sdStatus');
    if (statusEl) statusEl.textContent = 'Testing...';

    try {
        const res = await fetch('/api/sd-status');
        if (!res.ok) {
            if (statusEl) {
                statusEl.className = 'sd-status error';
                statusEl.textContent = '✗ Server returned ' + res.status;
            }
            return;
        }
        const data = await res.json();
        if (statusEl) {
            statusEl.className = `sd-status ${data.status === 'connected' ? 'ok' : 'error'}`;
            statusEl.textContent = data.status === 'connected'
                ? `✓ Server connected to ${data.host}`
                : `✗ ${data.error || 'Disconnected'}`;
        }
    } catch (err) {
        if (statusEl) {
            statusEl.className = 'sd-status error';
            statusEl.textContent = '✗ Connection failed: ' + err.message;
        }
    }
}
window.testSdConnection = testSdConnection;

function saveSdSettings() {
    // Saves the host to browser localStorage only — not persisted to the server.
    // The server uses its own configured SD host independent of this value.
    const host = document.getElementById('sdHostInput')?.value.trim() || '';
    if (host) localStorage.setItem('vetinari_sd_host', host);
    VApp.showStatusBanner('Image generation host saved to this browser (local only)', 'success');
}
window.saveSdSettings = saveSdSettings;

// ──────────────────────────────────────────────────────────────────────────────
// Password visibility toggle
// ──────────────────────────────────────────────────────────────────────────────

function togglePasswordVis(inputId, btn) {
    const input = document.getElementById(inputId);
    if (!input) return;
    if (input.type === 'password') {
        input.type = 'text';
        btn.innerHTML = '<i class="fas fa-eye-slash"></i>';
    } else {
        input.type = 'password';
        btn.innerHTML = '<i class="fas fa-eye"></i>';
    }
}
window.togglePasswordVis = togglePasswordVis;

// ──────────────────────────────────────────────────────────────────────────────
// Rules panel toggles
// ──────────────────────────────────────────────────────────────────────────────

document.getElementById('toggleRulesBtn')?.addEventListener('click', () => {
    const panel = document.getElementById('rulesPanel');
    if (panel) panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
});

// ──────────────────────────────────────────────────────────────────────────────
// Wire up the intake form submit button and "New Project" buttons
// ──────────────────────────────────────────────────────────────────────────────

document.getElementById('intakeSubmitBtn')?.addEventListener('click', submitIntakeForm);

// Override existing newProjectBtn to show the intake form
const _existingNewProjectBtn = document.getElementById('newProjectBtn');
if (_existingNewProjectBtn) {
    _existingNewProjectBtn.removeEventListener('click', _existingNewProjectBtn.__handler);
    _existingNewProjectBtn.addEventListener('click', showNewProjectForm);
}

// Keyboard shortcut: Ctrl+Enter to submit intake form
document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
        const intakeForm = document.getElementById('projectIntakeForm');
        if (intakeForm && intakeForm.style.display !== 'none') {
            submitIntakeForm();
            return;
        }
        // If chat input is focused
        const chatInput = document.getElementById('chatInput');
        if (document.activeElement === chatInput) {
            window.sendChatMessage?.();
        }
    }
});

// ──────────────────────────────────────────────────────────────────────────────
// Settings page: load rules and training data on view switch
// ──────────────────────────────────────────────────────────────────────────────

// Settings/prompt view hooks now live in the main switchView() in app.js
// (previously monkey-patched here — migrated for ARCH-02 fix)

async function loadSettingsRules() {
    try {
        const res = await fetch('/api/v1/rules');
        if (!res.ok) { console.warn('API error:', res.status); return; }
        const data = await res.json();

        const globalRulesInput = document.getElementById('globalRulesInput');
        if (globalRulesInput) globalRulesInput.value = (data.global || []).join('\n');

        const globalPromptInput = document.getElementById('globalSystemPromptInput');
        if (globalPromptInput) globalPromptInput.value = data.global_system_prompt || '';
    } catch (err) {
        console.error('Failed to load settings rules:', err);
    }
}

function loadSdSettings() {
    const host = localStorage.getItem('vetinari_sd_host') || 'http://localhost:7860';
    const el = document.getElementById('sdHostInput');
    if (el) el.value = host;
}

// ──────────────────────────────────────────────────────────────────────────────
// Initialize on page load
// ──────────────────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
    // Detect hardware for intake form
    detectHardware();

    // Populate model selects in intake form
    setTimeout(async () => {
        try {
            const res = await fetch('/api/v1/models');
            if (!res.ok) { console.warn('API error:', res.status); return; }
            const data = await res.json();
            const models = data.models || data;
            if (Array.isArray(models)) {
                const selects = [
                    document.getElementById('intakeModelSelect'),
                    document.getElementById('modelRulesModelSelect'),
                    document.getElementById('trainingModelSelect'),
                ];
                selects.forEach(sel => {
                    if (!sel) return;
                    models.forEach(m => {
                        const opt = document.createElement('option');
                        opt.value = m.id || m.name || m;
                        opt.textContent = m.name || m.id || m;
                        sel.appendChild(opt);
                    });
                });
            }
        } catch (e) { /* ignore */ }
    }, 1000);

    // Show intake flow if no project selected on prompt view
    if (window.location.hash === '#prompt' || document.querySelector('.nav-item.active[data-view="prompt"]')) {
        if (window.VApp && !VApp.currentProjectId && window.IntakeFlow) {
            window.IntakeFlow.show();
        }
    }
});

// Show intake flow by default when prompt view is first loaded
const promptViewEl = document.getElementById('promptView');
if (promptViewEl) {
    const observer = new MutationObserver(() => {
        if (promptViewEl.classList.contains('active') && !VApp.currentProjectId) {
            if (window.IntakeFlow) window.IntakeFlow.show();
            detectHardware();
        }
    });
    observer.observe(promptViewEl, { attributes: true, attributeFilter: ['class'] });
}

// ── Permission checks for gated actions ─────────────────────────────────
//
// Checks user preferences to determine if an action is allowed.
// Returns true if allowed, false if denied or user declines.

async function checkPermission(action) {
    var prefs;
    try {
        var res = await fetch('/api/v1/preferences');
        if (!res.ok) {
            // Fail closed when preferences cannot be loaded — server admin_guard
            // is the real authorization boundary, but the client gate must not
            // mislead the user into thinking an action is permitted.
            VToast.warning('Could not verify permissions — refresh and try again.');
            return false;
        }
        var data = await res.json();
        prefs = data.preferences || data;
    } catch (e) {
        VToast.warning('Could not verify permissions — refresh and try again.');
        return false;
    }

    var autonomyLevel = prefs.autonomyLevel || 'assisted';

    // Autonomous mode: always allow
    if (autonomyLevel === 'autonomous') return true;

    // Supervised mode: always ask
    if (autonomyLevel === 'supervised') {
        return await VModal.confirm(
            'This action requires permission: ' + _formatActionName(action) + '. Proceed?'
        );
    }

    // Assisted mode: check per-action overrides (ask | auto | deny)
    var allowKey = 'allow' + action;
    var actionPref = prefs[allowKey];

    // "auto" or true = auto-allow
    if (actionPref === 'auto' || actionPref === true) return true;

    // "deny" = block with warning toast
    if (actionPref === 'deny') {
        if (window.ToastManager) {
            ToastManager.show('Action denied by policy: ' + _formatActionName(action), 'warning');
        }
        return false;
    }

    // "ask" or false or unset = prompt the user
    if (actionPref === 'ask' || actionPref === false || actionPref == null) {
        return await VModal.confirm(
            'Allow ' + _formatActionName(action) + '?'
        );
    }

    // Fallback: allow
    return true;
}

function _formatActionName(action) {
    // "ModelDownload" -> "Model Download"
    return action.replace(/([A-Z])/g, ' $1').trim();
}

window.checkPermission = checkPermission;
