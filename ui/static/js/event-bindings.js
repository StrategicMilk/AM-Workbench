/**
 * Event Bindings
 * ==============
 * Replaces all inline onclick="" handlers with addEventListener calls.
 * CSP nonce policy blocks inline event handlers, so all click wiring
 * must happen here or in app.js / vetinari_extensions.js.
 *
 * Loaded AFTER app.js and vetinari_extensions.js.
 */

'use strict';

// Clipboard fallback for non-HTTPS or older browsers
function _fallbackCopyText(text) {
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.left = '-9999px';
    document.body.appendChild(ta);
    ta.select();
    try {
        document.execCommand('copy');
        if (typeof VToast !== 'undefined') VToast.success('Copied!');
    } catch (_) {
        if (typeof VToast !== 'undefined') VToast.error('Copy failed — use Ctrl+C');
    }
    document.body.removeChild(ta);
}

(function() {
    // Helper: bind click by ID, tolerating missing elements
    function bind(id, fn) {
        var el = document.getElementById(id);
        if (el) el.addEventListener('click', fn);
    }

    // ── Navigation ──────────────────────────────────────────────────────────
    bind('panelNewProjectBtn', function() { VApp.switchView('prompt'); });

    // ── Status banner ───────────────────────────────────────────────────────
    bind('statusBannerCloseBtn', function() { VApp.hideStatusBanner(); });

    // ── Dashboard: Agent Status & Shared Memory ─────────────────────────────
    bind('refreshAgentStatusBtn', function() { VApp.refreshAgentStatus(); });
    bind('refreshSharedMemoryBtn', function() { VApp.refreshSharedMemory(); });

    // ── Project Rules panel ─────────────────────────────────────────────────
    bind('loadGlobalRulesBtn', function() { loadGlobalRules(); });
    bind('saveProjectRulesBtn', function() { saveProjectRules(); });

    // ── Task list ───────────────────────────────────────────────────────────
    bind('expandAllTasksBtn', function() { VApp.expandAllTasks(); });
    bind('collapseAllTasksBtn', function() { VApp.collapseAllTasks(); });

    // ── Model ranking ───────────────────────────────────────────────────────
    bind('refreshModelSearchBtn', function() { VApp.refreshModelSearch(); });
    bind('applyModelOverrideBtn', function() { VApp.applyModelOverride(); });

    // ── Final deliverable ───────────────────────────────────────────────────
    bind('copyDeliverableBtn', function() { copyDeliverable(); });
    bind('approveDeliverableBtn', function() { approveDeliverable(); });
    bind('requestChangesBtn', function() { requestChanges(); });
    bind('approveAllBtn', function() { approveDeliverable(); });

    // ── Intake form ─────────────────────────────────────────────────────────
    bind('intakeClearBtn', function() { clearIntakeForm(); });
    bind('cancelProjectBtn', function() { VApp.cancelCurrentProject(); });
    bind('pauseProjectBtn', function() { pauseCurrentProject(); });
    bind('resumeProjectBtn', function() { resumeCurrentProject(); });

    // ── Agents view ─────────────────────────────────────────────────────────
    bind('initializeAgentsBtn', function() { VApp.initializeAgents(); });
    bind('decomposeAgentBtn', function() { VApp.decomposeWithAgent(); });
    bind('runAssignmentBtn', function() { VApp.runAssignmentPass(); });

    // ── Training view ───────────────────────────────────────────────────────
    bind('exportTrainingDataBtn', function() { VApp.exportTrainingData(); });
    bind('startTrainingBtn', function() { VApp.startTraining(); });
    bind('seedDataBtn', function() { VApp.seedTrainingData(); });

    // Phase 4: Training quick actions
    bind('quickStartTrainingBtn', function() { VApp.quickStartTraining(); });
    bind('quickCollectDataBtn', function() { VApp.quickCollectData(); });
    bind('quickViewLastRunBtn', function() { VApp.quickViewLastRun(); });

    // Phase 4: Training config & monitor
    bind('saveTrainingConfigBtn', function() { VApp.saveTrainingConfig(); });
    bind('dryRunTrainingBtn', function() { VApp.dryRunTraining(); });
    bind('pauseTrainingBtn', function() { VApp.pauseTraining(); });
    bind('stopTrainingBtn', function() { VApp.stopTraining(); });

    // Phase 4: Training data
    bind('trainingDataFilterBtn', function() { VApp.filterTrainingData(); });
    bind('generateSyntheticDataBtn', function() { VApp.generateSyntheticData(); });

    // Phase 4: Training automation
    bind('addTrainingRuleBtn', function() { VApp.addTrainingRule(); });
    bind('evolveNowBtn', function() { VApp.evolveNow(); });
    bind('pauseAllTrainingBtn', function() { VApp.pauseAllTraining(); });

    // Phase 4: Idle training toggle
    var idleToggle = document.getElementById('idleTrainingToggle');
    if (idleToggle) {
        idleToggle.addEventListener('change', function() { VApp.toggleIdleTraining(this.checked); });
    }

    // ── Dashboard ─────────────────────────────────────────────────────────
    bind('refreshDashboard', function() { VApp.loadDashboard(); });

    // ── Settings: custom instructions ─────────────────────────────────────
    bind('saveCustomInstructionsBtn', function() { VApp.saveCustomInstructions(); });

    // ── Settings: Reset Preferences ──────────────────────────────────────
    bind('resetPreferencesBtn', function() {
        if (typeof window.resetUserPatterns === 'function') {
            window.resetUserPatterns();
        }
        // Also clear localStorage UI preferences but keep setup and auth
        localStorage.removeItem('focusModeEnabled');
        document.documentElement.setAttribute('data-focus', 'false');
        var exitPill = document.getElementById('focusExitPill');
        if (exitPill) exitPill.style.display = 'none';
        if (window.ToastManager) {
            ToastManager.show('Preferences reset to defaults', 'success');
        }
    });

    // ── Settings: credentials ───────────────────────────────────────────────
    bind('toggleCredentialVisBtn', function() {
        togglePasswordVis('credentialTokenInput', this);
    });

    // ── Settings: rules ─────────────────────────────────────────────────────
    bind('saveGlobalRulesBtn', function() { saveGlobalRules(); });
    bind('loadModelRulesBtn', function() { loadModelRules(); });
    bind('saveModelRulesBtn', function() { saveModelRules(); });
    bind('saveGlobalSystemPromptBtn', function() { saveGlobalSystemPrompt(); });

    // ── Settings: Stable Diffusion ──────────────────────────────────────────
    bind('testSdConnectionBtn', function() { testSdConnection(); });
    bind('saveSdSettingsBtn', function() { saveSdSettings(); });

    // ── Memory view ──────────────────────────────────────────────────────────
    bind('refreshMemoryBtn', function() { VApp.loadMemory(); });
    bind('memorySearchBtn', function() { VApp.searchMemory(); });
    bind('addMemoryBtn', function() { VApp.addMemoryEntry(); });
    var memSearchInput = document.getElementById('memorySearchInput');
    if (memSearchInput) {
        memSearchInput.addEventListener('keydown', function(e) {
            if (e.key === 'Enter') { e.preventDefault(); VApp.searchMemory(); }
        });
    }

    // Memory filter change events
    var memTypeFilter = document.getElementById('memoryTypeFilter');
    if (memTypeFilter) {
        memTypeFilter.addEventListener('change', function() { VApp.loadMemory(); });
    }
    var memAgentFilter = document.getElementById('memoryAgentFilter');
    if (memAgentFilter) {
        memAgentFilter.addEventListener('change', function() { VApp.loadMemory(); });
    }

    // ── Tab switching (generic) ──────────────────────────────────────────────
    document.querySelectorAll('.view-tabs').forEach(function(tabBar) {
        tabBar.addEventListener('click', function(e) {
            var tab = e.target.closest('.view-tab');
            if (!tab) return;
            var tabId = tab.dataset.tab;
            if (!tabId) return;

            // Deactivate all tabs in this tab bar
            tabBar.querySelectorAll('.view-tab').forEach(function(t) {
                t.classList.remove('active');
                t.setAttribute('aria-selected', 'false');
            });
            tab.classList.add('active');
            tab.setAttribute('aria-selected', 'true');

            // Show/hide tab content panels within the same view
            var view = tabBar.closest('.view');
            if (view) {
                view.querySelectorAll('.view-tab-content').forEach(function(panel) {
                    panel.classList.toggle('active', panel.id === tabId);
                });
            }

            // Data loading hooks for specific tabs
            if (tabId === 'models-recommended' && typeof VApp !== 'undefined' && VApp.loadModelsRecommended) {
                VApp.loadModelsRecommended();
            }
            // Memory type filter tabs
            if (tab.dataset.memoryType !== undefined && tab.dataset.memoryType !== '_stats' && typeof VApp !== 'undefined' && VApp.loadMemoryFiltered) {
                VApp.loadMemoryFiltered(tab.dataset.memoryType, tabId);
            }
        });
    });

    // ── Memory: actions via event delegation ──
    document.addEventListener('click', function(e) {
        var deleteBtn = e.target.closest('[data-action="deleteMemory"]');
        if (deleteBtn) {
            VApp.deleteMemory(deleteBtn.dataset.id);
            return;
        }
        var nextBtn = e.target.closest('[data-action="memoryNextPage"]');
        if (nextBtn) {
            VApp.memoryNextPage();
            return;
        }
        var prevBtn = e.target.closest('[data-action="memoryPrevPage"]');
        if (prevBtn) {
            VApp.memoryPrevPage();
            return;
        }
    });

    // ── Chat message actions (event delegation) ───────────────────────────
    document.addEventListener('click', function(e) {
        var actionBtn = e.target.closest('[data-action]');
        if (!actionBtn) return;
        var action = actionBtn.dataset.action;
        var msgEl = actionBtn.closest('.chat-message');

        if (action === 'copyMsg' && msgEl) {
            var contentEl = msgEl.querySelector('.chat-message-content');
            if (contentEl) {
                var text = contentEl.textContent;
                if (navigator.clipboard && navigator.clipboard.writeText) {
                    navigator.clipboard.writeText(text).then(function() {
                        if (typeof VToast !== 'undefined') VToast.success('Copied!');
                    }).catch(function() {
                        _fallbackCopyText(text);
                    });
                } else {
                    _fallbackCopyText(text);
                }
            }
            return;
        }
        if (action === 'retryMsg' && msgEl) {
            // Find the preceding user message and re-send it
            var prev = msgEl.previousElementSibling;
            while (prev && !prev.classList.contains('user')) {
                prev = prev.previousElementSibling;
            }
            if (prev) {
                var userContent = prev.querySelector('.chat-message-content');
                if (userContent) {
                    var chatInput = document.getElementById('chatInput');
                    if (chatInput) {
                        chatInput.value = userContent.textContent;
                        VApp.sendChatMessage();
                    }
                }
            }
            return;
        }
        if (action === 'feedbackUp' || action === 'feedbackDown') {
            // Toggle active state
            actionBtn.classList.toggle('active');
            // Deactivate the other feedback button
            var siblingAction = action === 'feedbackUp' ? 'feedbackDown' : 'feedbackUp';
            var sibling = msgEl.querySelector('[data-action="' + siblingAction + '"]');
            if (sibling) sibling.classList.remove('active');
            // Store feedback (fire-and-forget)
            var rating = action === 'feedbackUp' ? 1 : -1;
            fetch('/api/v1/chat/feedback', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
                body: JSON.stringify({ rating: rating, project_id: VApp.currentProjectId || '' })
            }).catch(function() { /* ignore if endpoint not ready */ });
            return;
        }
        if (action === 'editPrompt' && msgEl) {
            // Find the preceding user message and put its text in the chat input for editing
            var prev = msgEl.previousElementSibling;
            while (prev && !prev.classList.contains('user')) {
                prev = prev.previousElementSibling;
            }
            if (prev) {
                var userContent = prev.querySelector('.chat-message-content');
                if (userContent) {
                    var chatInput = document.getElementById('chatInput');
                    if (chatInput) {
                        chatInput.value = userContent.textContent;
                        chatInput.focus();
                        chatInput.setSelectionRange(chatInput.value.length, chatInput.value.length);
                    }
                }
            }
            return;
        }
    });

    // ── Context panel toggle ─────────────────────────────────────────────
    bind('contextPanelToggle', function() {
        var panel = document.getElementById('contextPanel');
        if (panel) panel.classList.toggle('open');
    });
    bind('contextPanelClose', function() {
        var panel = document.getElementById('contextPanel');
        if (panel) panel.classList.remove('open');
    });

    // ── Export conversation dropdown ─────────────────────────────────────
    bind('exportConversationBtn', function(e) {
        e.stopPropagation();
        var dropdown = document.getElementById('exportDropdown');
        if (dropdown) {
            dropdown.style.display = dropdown.style.display === 'none' ? 'block' : 'none';
        }
    });
    // Close export dropdown on click outside
    document.addEventListener('click', function(e) {
        var dropdown = document.getElementById('exportDropdown');
        if (dropdown && !e.target.closest('.export-btn-wrapper')) {
            dropdown.style.display = 'none';
        }
    });
    // Export format handlers
    document.addEventListener('click', function(e) {
        var exportItem = e.target.closest('.export-item');
        if (!exportItem) return;
        var format = exportItem.dataset.format;
        var messages = document.querySelectorAll('#chatMessages .chat-message');
        if (!messages.length) return;

        var output = '';
        var jsonData = [];
        messages.forEach(function(msg) {
            var role = msg.classList.contains('user') ? 'user' : 'assistant';
            var content = msg.querySelector('.chat-message-content')?.textContent || '';
            var agentBadge = msg.querySelector('.agent-badge');
            var agent = agentBadge ? agentBadge.textContent.trim() : '';
            var timeEl = msg.querySelector('.msg-time');
            var timestamp = timeEl ? timeEl.textContent.trim() : '';

            if (format === 'md') {
                if (role === 'user') {
                    output += '> **You**: ' + content.trim() + '\n\n';
                } else {
                    output += '### ' + (agent || 'Vetinari') + (timestamp ? ' (' + timestamp + ')' : '') + '\n\n';
                    output += content.trim() + '\n\n---\n\n';
                }
            } else if (format === 'txt') {
                output += '[' + role.toUpperCase() + '] ' + content.trim() + '\n\n';
            } else if (format === 'json') {
                jsonData.push({ role: role, content: content.trim(), agent: agent, timestamp: timestamp });
            }
        });

        var blob;
        var filename = 'conversation';
        if (format === 'json') {
            blob = new Blob([JSON.stringify(jsonData, null, 2)], { type: 'application/json' });
            filename += '.json';
        } else if (format === 'md') {
            blob = new Blob([output], { type: 'text/markdown' });
            filename += '.md';
        } else {
            blob = new Blob([output], { type: 'text/plain' });
            filename += '.txt';
        }

        var link = document.createElement('a');
        link.href = URL.createObjectURL(blob);
        link.download = filename;
        link.click();
        URL.revokeObjectURL(link.href);

        var dropdown2 = document.getElementById('exportDropdown');
        if (dropdown2) dropdown2.style.display = 'none';
        if (typeof VToast !== 'undefined') VToast.success('Exported as ' + format.toUpperCase());
    });

    // ── Advanced intake dialog ───────────────────────────────────────────
    bind('advancedIntakeClose', function() {
        var dialog = document.getElementById('advancedIntakeDialog');
        if (dialog && typeof dialog.close === 'function') dialog.close();
    });

    // ── Auto-growing chat input ──────────────────────────────────────────
    var chatInputEl = document.getElementById('chatInput');
    if (chatInputEl) {
        chatInputEl.addEventListener('input', function() {
            this.style.height = 'auto';
            this.style.height = Math.min(this.scrollHeight, 200) + 'px';
            // Auto-save draft to localStorage
            try { localStorage.setItem('vetinari_chat_draft', this.value); } catch(e) { /* ignore */ }
        });
        // Restore draft on load
        try {
            var draft = localStorage.getItem('vetinari_chat_draft');
            if (draft) {
                chatInputEl.value = draft;
                chatInputEl.style.height = 'auto';
                chatInputEl.style.height = Math.min(chatInputEl.scrollHeight, 200) + 'px';
            }
        } catch(e) { /* ignore */ }
    }

    // ── File attachments ─────────────────────────────────────────────────
    bind('attachFileBtn', function() {
        var picker = document.getElementById('filePickerInput');
        if (picker) picker.click();
    });

    var filePickerInput = document.getElementById('filePickerInput');
    if (filePickerInput) {
        filePickerInput.addEventListener('change', function() {
            addAttachmentFiles(this.files);
            this.value = '';
        });
    }

    // Drag-and-drop on chat input area
    var chatInputArea = document.getElementById('chatInputArea');
    if (chatInputArea) {
        chatInputArea.addEventListener('dragover', function(e) {
            e.preventDefault();
            chatInputArea.classList.add('drag-over');
        });
        chatInputArea.addEventListener('dragleave', function(e) {
            if (!chatInputArea.contains(e.relatedTarget)) {
                chatInputArea.classList.remove('drag-over');
            }
        });
        chatInputArea.addEventListener('drop', function(e) {
            e.preventDefault();
            chatInputArea.classList.remove('drag-over');
            if (e.dataTransfer.files.length > 0) {
                addAttachmentFiles(e.dataTransfer.files);
            }
        });
    }

    // Paste handler for images
    if (chatInputEl) {
        chatInputEl.addEventListener('paste', function(e) {
            var items = e.clipboardData?.items;
            if (!items) return;
            for (var i = 0; i < items.length; i++) {
                if (items[i].type.startsWith('image/')) {
                    var file = items[i].getAsFile();
                    if (file) addAttachmentFiles([file]);
                }
            }
        });
    }

    // ── Prompt presets (/ commands) ───────────────────────────────────────
    if (chatInputEl) {
        chatInputEl.addEventListener('input', function() {
            handlePresetInput(this);
        });
        chatInputEl.addEventListener('keydown', function(e) {
            handlePresetKeydown(e, this);
        });
    }

    // ── Project sidebar search ───────────────────────────────────────────
    var projectSearchInput = document.getElementById('projectSearchInput');
    if (projectSearchInput) {
        projectSearchInput.addEventListener('input', function() {
            filterProjectSidebar(this.value.trim().toLowerCase());
        });
    }

    // ── Project sidebar right-click context menu ─────────────────────────
    var projectsList = document.getElementById('projectsList');
    if (projectsList) {
        projectsList.addEventListener('contextmenu', function(e) {
            var item = e.target.closest('.project-item');
            if (!item) return;
            e.preventDefault();
            showProjectContextMenu(e.clientX, e.clientY, item);
        });
    }
    // Close context menu on click outside
    document.addEventListener('click', function() {
        var menu = document.querySelector('.context-menu');
        if (menu) menu.remove();
    });

    // ── Text selection quick actions ─────────────────────────────────────
    document.addEventListener('mouseup', function(e) {
        // Remove existing popover
        var existing = document.querySelector('.selection-popover');
        if (existing) existing.remove();

        var sel = window.getSelection();
        var text = sel ? sel.toString().trim() : '';
        if (!text || text.length < 3) return;

        // Only show for selections within chat messages
        var msgEl = e.target.closest('.chat-message.assistant .chat-message-content');
        if (!msgEl) return;

        var range = sel.getRangeAt(0);
        var rect = range.getBoundingClientRect();

        var popover = document.createElement('div');
        popover.className = 'selection-popover';
        popover.innerHTML = '<button data-sel-action="explain">Explain this</button>' +
            '<button data-sel-action="ask">Ask about this</button>' +
            '<button data-sel-action="copy">Copy</button>';
        popover.style.left = rect.left + 'px';
        popover.style.top = (rect.top - 40) + 'px';
        document.body.appendChild(popover);

        popover.addEventListener('click', function(ev) {
            var btn = ev.target.closest('[data-sel-action]');
            if (!btn) return;
            var action = btn.dataset.selAction;
            var input = document.getElementById('chatInput');
            if (action === 'explain' && input) {
                input.value = 'Explain this: "' + text + '"';
                VApp.sendChatMessage();
            } else if (action === 'ask' && input) {
                input.value = text;
                input.focus();
            } else if (action === 'copy') {
                if (navigator.clipboard && navigator.clipboard.writeText) {
                    navigator.clipboard.writeText(text).then(function() {
                        if (typeof VToast !== 'undefined') VToast.success('Copied!');
                    }).catch(function() { _fallbackCopyText(text); });
                } else {
                    _fallbackCopyText(text);
                }
            }
            popover.remove();
        });
    });

    // ── Server control ──────────────────────────────────────────────────────
    bind('shutdownServerBtn', function() {
        if (typeof VModal === 'undefined') return;
        VModal.confirm('Shut down the Vetinari server? You will need to restart it manually.').then(function(ok) {
            if (!ok) return;
            var statusDot = document.getElementById('connectionStatusDot');
            var statusText = document.getElementById('serverStatusText');
            if (statusText) statusText.textContent = 'Shutting down...';
            if (statusDot) statusDot.classList.add('status-shutting-down');

            fetch('/api/v1/server/shutdown', { method: 'POST' })
                .then(function(res) {
                    if (!res.ok) {
                        throw new Error('shutdown route unavailable: ' + res.status);
                    }
                    return res.json();
                })
                .then(function(data) {
                    if (data.error) {
                        VApp.showStatusBanner('Shutdown failed: ' + data.error, 'error');
                        if (statusText) statusText.textContent = 'System Online';
                        if (statusDot) statusDot.classList.remove('status-shutting-down');
                        return;
                    }
                    VApp.showStatusBanner('Server is shutting down...', 'warning');
                    if (statusText) statusText.textContent = 'Offline';
                    if (statusDot) {
                    statusDot.classList.remove('status-shutting-down');
                    statusDot.classList.add('status-offline');
                }
            })
                .catch(function(err) {
                    VApp.showStatusBanner('Shutdown unavailable: ' + err.message, 'error');
                    if (statusText) statusText.textContent = 'Shutdown unavailable';
                    if (statusDot) {
                        statusDot.classList.remove('status-shutting-down');
                        statusDot.classList.add('status-indicator--error');
                    }
                });
        });
    });

    // ══════════════════════════════════════════════════════════════════════
    //  Helper functions (used by event bindings above)
    // ══════════════════════════════════════════════════════════════════════

    // ── Attachment management ────────────────────────────────────────────

    var attachments = [];
    var ALLOWED_EXTENSIONS = ['.py', '.js', '.ts', '.json', '.yaml', '.yml', '.md', '.txt', '.png', '.jpg', '.jpeg'];

    function addAttachmentFiles(files) {
        var bar = document.getElementById('attachmentBar');
        if (!bar) return;

        for (var i = 0; i < files.length; i++) {
            var file = files[i];
            var ext = '.' + file.name.split('.').pop().toLowerCase();
            if (ALLOWED_EXTENSIONS.indexOf(ext) === -1) {
                if (typeof VToast !== 'undefined') VToast.warning('Unsupported file type: ' + ext);
                continue;
            }
            var idx = attachments.length;
            attachments.push(file);

            var chip = document.createElement('div');
            chip.className = 'attachment-chip';
            chip.dataset.idx = idx;

            var isImage = file.type.startsWith('image/');
            if (isImage) {
                var img = document.createElement('img');
                img.className = 'attachment-thumb';
                img.alt = file.name;
                var reader = new FileReader();
                reader.onload = (function(imgEl) {
                    return function(ev) { imgEl.src = ev.target.result; };
                })(img);
                reader.readAsDataURL(file);
                chip.appendChild(img);
            } else {
                var icon = document.createElement('i');
                icon.className = 'fas fa-file';
                icon.style.fontSize = '12px';
                chip.appendChild(icon);
            }

            var nameSpan = document.createElement('span');
            nameSpan.className = 'attachment-chip-name';
            nameSpan.textContent = file.name;
            chip.appendChild(nameSpan);

            var removeBtn = document.createElement('button');
            removeBtn.className = 'attachment-chip-remove';
            removeBtn.innerHTML = '<i class="fas fa-times"></i>';
            removeBtn.dataset.idx = idx;
            removeBtn.addEventListener('click', function(ev) {
                ev.stopPropagation();
                var chipEl = ev.target.closest('.attachment-chip');
                if (chipEl) {
                    attachments[parseInt(chipEl.dataset.idx)] = null;
                    chipEl.remove();
                }
            });
            chip.appendChild(removeBtn);
            bar.appendChild(chip);
        }
    }
    window.addAttachmentFiles = addAttachmentFiles;
    window.getAttachments = function() {
        return attachments.filter(function(a) { return a !== null; });
    };

    // ── Prompt presets ───────────────────────────────────────────────────

    var PRESETS = [
        { name: '/analyze', desc: 'Analyze code for bugs and improvements', template: 'Analyze the following code for bugs and improvements:\n' },
        { name: '/explain', desc: 'Explain in simple terms', template: 'Explain the following in simple terms:\n' },
        { name: '/test', desc: 'Write comprehensive tests', template: 'Write comprehensive tests for:\n' },
        { name: '/security', desc: 'Perform a security audit', template: 'Perform a security audit on:\n' },
        { name: '/refactor', desc: 'Refactor for clarity and performance', template: 'Refactor the following for clarity and performance:\n' },
        { name: '/docs', desc: 'Write documentation', template: 'Write documentation for:\n' }
    ];
    var presetHighlightIdx = -1;

    function handlePresetInput(inputEl) {
        var menu = document.getElementById('presetMenu');
        if (!menu) return;

        var val = inputEl.value;
        if (val === '/' || (val.startsWith('/') && val.indexOf(' ') === -1)) {
            var filter = val.toLowerCase();
            var matches = PRESETS.filter(function(p) {
                return p.name.startsWith(filter);
            });
            if (matches.length > 0) {
                var html = '';
                matches.forEach(function(p, i) {
                    html += '<button class="preset-item' + (i === 0 ? ' highlighted' : '') + '" data-preset="' + p.name + '">' +
                        '<span class="preset-name">' + p.name + '</span>' +
                        '<span class="preset-desc">' + p.desc + '</span></button>';
                });
                menu.innerHTML = html;
                menu.style.display = 'block';
                presetHighlightIdx = 0;

                menu.querySelectorAll('.preset-item').forEach(function(item) {
                    item.addEventListener('click', function() {
                        var preset = PRESETS.find(function(pr) { return pr.name === item.dataset.preset; });
                        if (preset) {
                            inputEl.value = preset.template;
                            inputEl.focus();
                            inputEl.setSelectionRange(inputEl.value.length, inputEl.value.length);
                        }
                        menu.style.display = 'none';
                    });
                });
            } else {
                menu.style.display = 'none';
            }
        } else {
            menu.style.display = 'none';
        }
    }

    function handlePresetKeydown(e, inputEl) {
        var menu = document.getElementById('presetMenu');
        if (!menu || menu.style.display === 'none') return;

        var items = menu.querySelectorAll('.preset-item');
        if (items.length === 0) return;

        if (e.key === 'ArrowDown') {
            e.preventDefault();
            presetHighlightIdx = Math.min(presetHighlightIdx + 1, items.length - 1);
            items.forEach(function(it, i) { it.classList.toggle('highlighted', i === presetHighlightIdx); });
        } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            presetHighlightIdx = Math.max(presetHighlightIdx - 1, 0);
            items.forEach(function(it, i) { it.classList.toggle('highlighted', i === presetHighlightIdx); });
        } else if (e.key === 'Enter' && !e.ctrlKey) {
            if (presetHighlightIdx >= 0 && presetHighlightIdx < items.length) {
                e.preventDefault();
                items[presetHighlightIdx].click();
            }
        } else if (e.key === 'Escape') {
            menu.style.display = 'none';
        }
    }

    // ── Project sidebar filtering ────────────────────────────────────────

    function filterProjectSidebar(query) {
        var items = document.querySelectorAll('#projectsList .project-item');
        items.forEach(function(item) {
            var text = item.textContent.toLowerCase();
            item.style.display = text.indexOf(query) !== -1 ? '' : 'none';
        });
    }

    // ── Project context menu ─────────────────────────────────────────────

    function showProjectContextMenu(x, y, projectItem) {
        // Remove existing menu
        var existing = document.querySelector('.context-menu');
        if (existing) existing.remove();

        var projectId = projectItem.dataset.id || '';
        var isPinned = projectItem.classList.contains('pinned');

        var menu = document.createElement('div');
        menu.className = 'context-menu';
        menu.style.left = x + 'px';
        menu.style.top = y + 'px';

        menu.innerHTML =
            '<button class="context-menu-item" data-ctx="rename"><i class="fas fa-edit"></i> Rename</button>' +
            '<button class="context-menu-item" data-ctx="pin"><i class="fas fa-thumbtack"></i> ' + (isPinned ? 'Unpin' : 'Pin') + '</button>' +
            '<button class="context-menu-item" data-ctx="export"><i class="fas fa-download"></i> Export</button>' +
            '<div class="context-menu-divider"></div>' +
            '<button class="context-menu-item danger" data-ctx="delete"><i class="fas fa-trash"></i> Delete</button>';

        document.body.appendChild(menu);

        // Keep menu within viewport
        var rect = menu.getBoundingClientRect();
        if (rect.right > window.innerWidth) menu.style.left = (window.innerWidth - rect.width - 8) + 'px';
        if (rect.bottom > window.innerHeight) menu.style.top = (window.innerHeight - rect.height - 8) + 'px';

        menu.addEventListener('click', function(ev) {
            var btn = ev.target.closest('[data-ctx]');
            if (!btn) return;
            var ctxAction = btn.dataset.ctx;

            if (ctxAction === 'rename') {
                var nameEl = projectItem.querySelector('.project-name') || projectItem;
                if (typeof VModal !== 'undefined' && VModal.prompt) {
                    VModal.prompt('Rename project:', nameEl.textContent.trim(), 'Rename').then(function(newName) {
                        if (newName && newName.trim()) {
                            nameEl.textContent = newName.trim();
                            if (projectId) {
                                fetch('/api/project/' + projectId + '/rename', {
                                    method: 'PUT',
                                    headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
                                    body: JSON.stringify({ name: newName.trim() })
                                }).catch(function() { /* ignore */ });
                            }
                        }
                    });
                }
            } else if (ctxAction === 'pin') {
                projectItem.classList.toggle('pinned');
                // Save pinned state to localStorage
                var pinned = JSON.parse(localStorage.getItem('vetinari_pinned_projects') || '[]');
                if (projectItem.classList.contains('pinned')) {
                    if (pinned.indexOf(projectId) === -1) pinned.push(projectId);
                    // Move to top of list
                    var list = projectItem.parentElement;
                    if (list) list.insertBefore(projectItem, list.firstChild);
                } else {
                    pinned = pinned.filter(function(id) { return id !== projectId; });
                }
                localStorage.setItem('vetinari_pinned_projects', JSON.stringify(pinned));
            } else if (ctxAction === 'export') {
                // Click the export button to use existing export flow
                var exportBtn = document.getElementById('exportConversationBtn');
                if (exportBtn) exportBtn.click();
            } else if (ctxAction === 'delete') {
                if (typeof VModal !== 'undefined' && VModal.confirm) {
                    VModal.confirm('Delete this project? This cannot be undone.', 'Delete Project').then(function(confirmed) {
                        if (!confirmed) return;
                        if (projectId) {
                            fetch('/api/project/' + projectId, {
                                method: 'DELETE',
                                headers: { 'X-Requested-With': 'XMLHttpRequest' }
                            })
                                .then(function(r) {
                                    if (!r.ok) throw new Error('Server returned ' + r.status);
                                    projectItem.remove();
                                    if (VApp.currentProjectId === projectId) {
                                        VApp.currentProjectId = null;
                                        if (window.IntakeFlow) window.IntakeFlow.show();
                                    }
                                    if (typeof VToast !== 'undefined') VToast.success('Project deleted');
                                })
                                .catch(function() {
                                    if (typeof VToast !== 'undefined') VToast.error('Failed to delete project');
                                });
                        }
                    });
                }
            }
            menu.remove();
        });
    }

    // ── Phase 4: Slider value displays ──────────────────────────────────────
    function wireSlider(sliderId, displayId, suffix, transform) {
        var slider = document.getElementById(sliderId);
        var display = document.getElementById(displayId);
        if (slider && display) {
            slider.addEventListener('input', function() {
                display.textContent = transform ? transform(slider.value) : slider.value + (suffix || '');
            });
        }
    }

    wireSlider('trainingMinQuality', 'trainingMinQualityValue', '%');
    wireSlider('trainValSplit', 'trainValSplitValue', '', function(v) { return v + '/' + (100 - parseInt(v)); });
    wireSlider('maxVramSlider', 'maxVramValue', '%');
    wireSlider('autoRevertThreshold', 'autoRevertValue', '%');
    wireSlider('fontSizeSlider', 'fontSizeValue', 'px');

    // ── Phase 4: Accent color swatches ──────────────────────────────────────
    var swatchContainer = document.getElementById('accentColorSwatches');
    if (swatchContainer) {
        swatchContainer.addEventListener('click', function(e) {
            var swatch = e.target.closest('.accent-swatch');
            if (!swatch) return;
            swatchContainer.querySelectorAll('.accent-swatch').forEach(function(s) { s.classList.remove('active'); });
            swatch.classList.add('active');
            var color = swatch.dataset.color;
            if (color) {
                document.documentElement.style.setProperty('--primary', color);
                localStorage.setItem('vetinari_accent', color);
            }
        });
    }

    // ── Phase 4: Pill selectors (generic) ───────────────────────────────────
    document.querySelectorAll('.pill-selector').forEach(function(selector) {
        selector.addEventListener('click', function(e) {
            var pill = e.target.closest('.pill');
            if (!pill) return;
            selector.querySelectorAll('.pill').forEach(function(p) { p.classList.remove('active'); });
            pill.classList.add('active');

            // Permission pill selectors (data-perm): persist to server preferences
            var permKey = selector.dataset.perm;
            if (permKey && pill.dataset.value) {
                var prefValue = pill.dataset.value; // "ask" | "auto" | "deny"
                var body = {};
                body[permKey] = prefValue;
                fetch('/api/v1/preferences', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
                    body: JSON.stringify(body)
                }).catch(function() {
                    // Silently fail — preference will be re-read from server on next check
                });
            }

            // Autonomy level pill selector: persist to server
            var field = selector.dataset.field;
            if (field === 'autonomyLevel' && pill.dataset.value) {
                var autonomyBody = { autonomyLevel: pill.dataset.value };
                fetch('/api/v1/preferences', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
                    body: JSON.stringify(autonomyBody)
                }).catch(function() {});
            }

            // Theme selector: apply theme and persist
            if (selector.id === 'themeSelector' && pill.dataset.value) {
                var theme = pill.dataset.value;
                document.documentElement.setAttribute('data-theme', theme);
                localStorage.setItem('theme', theme);
                // Update the header theme toggle icon to match
                if (typeof VApp !== 'undefined' && VApp.updateThemeIcon) {
                    VApp.updateThemeIcon(theme);
                } else {
                    var themeBtn = document.getElementById('themeToggle');
                    if (themeBtn) {
                        var icon = themeBtn.querySelector('i');
                        if (icon) icon.className = theme === 'dark' ? 'fas fa-moon' : 'fas fa-sun';
                    }
                }
                fetch('/api/v1/preferences', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
                    body: JSON.stringify({ theme: theme })
                }).catch(function() {});
            }

            // Interface mode selector: persist and apply immediately
            if (selector.id === 'interfaceModeSelector' && pill.dataset.value) {
                var mode = pill.dataset.value;
                if (typeof VApp !== 'undefined' && VApp.applyInterfaceMode) {
                    VApp.applyInterfaceMode(mode);
                }
                fetch('/api/v1/preferences', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
                    body: JSON.stringify({ interfaceMode: mode })
                }).catch(function() {});
                // Update description text
                var desc = document.getElementById('interfaceModeDesc');
                if (desc) {
                    var descriptions = {
                        simple: 'Clean chat-only view, auto-configuration, minimal settings.',
                        standard: 'Full navigation, all views accessible, guided configuration.',
                        expert: 'Everything plus raw config editors, debug logging, all advanced options.'
                    };
                    desc.textContent = descriptions[mode] || descriptions.standard;
                }
            }
        });
    });

    // ══════════════════════════════════════════════════════════════════════
    //  Keyboard Shortcut System
    //  Centralized global shortcuts. Does NOT fire when user is typing in
    //  an input/textarea/contenteditable, except for the whitelisted keys.
    // ══════════════════════════════════════════════════════════════════════

    function isTypingContext(e) {
        // Returns true when the user is actively typing in an editable element.
        var tag = e.target && e.target.tagName;
        if (tag === 'INPUT' || tag === 'TEXTAREA') return true;
        if (e.target && e.target.isContentEditable) return true;
        return false;
    }

    function showShortcutOverlay() {
        // Remove any existing overlay
        var existing = document.querySelector('.shortcuts-modal');
        if (existing) existing.remove();

        var backdrop = document.createElement('div');
        backdrop.className = 'modal-backdrop shortcuts-modal';
        backdrop.innerHTML =
            '<div class="modal" role="dialog" aria-modal="true" aria-label="Keyboard Shortcuts">'
            + '<div class="modal-header">'
            + '<h2>Keyboard Shortcuts</h2>'
            + '<button class="modal-close" aria-label="Close">&times;</button>'
            + '</div>'
            + '<div class="modal-body">'

            // Navigation group
            + '<div class="shortcut-group">'
            + '<div class="shortcut-group__title">Navigation</div>'
            + '<div class="shortcut-row"><span class="shortcut-row__action">Switch to Chat</span><span class="shortcut-row__keys"><kbd>Ctrl</kbd><kbd>1</kbd></span></div>'
            + '<div class="shortcut-row"><span class="shortcut-row__action">Switch to Models</span><span class="shortcut-row__keys"><kbd>Ctrl</kbd><kbd>2</kbd></span></div>'
            + '<div class="shortcut-row"><span class="shortcut-row__action">Switch to Training</span><span class="shortcut-row__keys"><kbd>Ctrl</kbd><kbd>3</kbd></span></div>'
            + '<div class="shortcut-row"><span class="shortcut-row__action">Switch to Memory</span><span class="shortcut-row__keys"><kbd>Ctrl</kbd><kbd>4</kbd></span></div>'
            + '<div class="shortcut-row"><span class="shortcut-row__action">Switch to Settings</span><span class="shortcut-row__keys"><kbd>Ctrl</kbd><kbd>5</kbd></span></div>'
            + '<div class="shortcut-row"><span class="shortcut-row__action">Switch to Dashboard</span><span class="shortcut-row__keys"><kbd>Ctrl</kbd><kbd>6</kbd></span></div>'
            + '<div class="shortcut-row"><span class="shortcut-row__action">Open Command Palette</span><span class="shortcut-row__keys"><kbd>Ctrl</kbd><kbd>K</kbd></span></div>'
            + '<div class="shortcut-row"><span class="shortcut-row__action">New Conversation</span><span class="shortcut-row__keys"><kbd>Ctrl</kbd><kbd>N</kbd></span></div>'
            + '</div>'

            // Chat group
            + '<div class="shortcut-group">'
            + '<div class="shortcut-group__title">Chat</div>'
            + '<div class="shortcut-row"><span class="shortcut-row__action">Send Message</span><span class="shortcut-row__keys"><kbd>Ctrl</kbd><kbd>Enter</kbd></span></div>'
            + '<div class="shortcut-row"><span class="shortcut-row__action">Focus Chat Input</span><span class="shortcut-row__keys"><kbd>Ctrl</kbd><kbd>/</kbd></span></div>'
            + '</div>'

            // Panels group
            + '<div class="shortcut-group">'
            + '<div class="shortcut-group__title">Panels</div>'
            + '<div class="shortcut-row"><span class="shortcut-row__action">Toggle Context Panel</span><span class="shortcut-row__keys"><kbd>Ctrl</kbd><kbd>Shift</kbd><kbd>E</kbd></span></div>'
            + '<div class="shortcut-row"><span class="shortcut-row__action">Toggle Sidebar</span><span class="shortcut-row__keys"><kbd>Ctrl</kbd><kbd>B</kbd></span></div>'
            + '</div>'

            // Settings group
            + '<div class="shortcut-group">'
            + '<div class="shortcut-group__title">Settings</div>'
            + '<div class="shortcut-row"><span class="shortcut-row__action">Toggle Theme</span><span class="shortcut-row__keys"><kbd>Ctrl</kbd><kbd>Shift</kbd><kbd>T</kbd></span></div>'
            + '</div>'

            // General group
            + '<div class="shortcut-group">'
            + '<div class="shortcut-group__title">General</div>'
            + '<div class="shortcut-row"><span class="shortcut-row__action">Close / Dismiss</span><span class="shortcut-row__keys"><kbd>Esc</kbd></span></div>'
            + '<div class="shortcut-row"><span class="shortcut-row__action">Show This Help</span><span class="shortcut-row__keys"><kbd>Ctrl</kbd><kbd>?</kbd></span></div>'
            + '</div>'

            + '</div>'
            + '<div class="modal-footer"></div>'
            + '</div>';

        document.body.appendChild(backdrop);

        var modal = backdrop.querySelector('.modal');
        var closeBtn = backdrop.querySelector('.modal-close');

        // Use AccessibilityManager.trapFocus for focus trapping and restore
        var cleanupTrap = null;
        if (window.AccessibilityManager) {
            cleanupTrap = window.AccessibilityManager.trapFocus(modal);
        } else if (closeBtn) {
            closeBtn.focus();
        }

        function closeOverlay() {
            if (cleanupTrap) cleanupTrap();
            backdrop.remove();
            document.removeEventListener('keydown', overlayEscHandler);
        }

        function overlayEscHandler(e) {
            if (e.key === 'Escape') {
                e.preventDefault();
                closeOverlay();
            }
        }

        closeBtn.addEventListener('click', closeOverlay);
        backdrop.addEventListener('click', function(e) {
            if (e.target === backdrop) closeOverlay();
        });
        document.addEventListener('keydown', overlayEscHandler);
    }

    // Expose globally so the command palette registration below can reference it.
    window.showShortcutOverlay = showShortcutOverlay;

    document.addEventListener('keydown', function(e) {
        var ctrl = e.ctrlKey || e.metaKey;

        // ── Escape — priority chain (always fires) ──
        if (e.key === 'Escape') {
            // (1) Command palette takes priority — handled in command-palette.js
            if (window.CommandPalette && typeof window.CommandPalette.close === 'function') {
                // Check if palette is open; command-palette.js already handles this,
                // but we handle the remaining cases here only when palette is NOT open.
                var paletteBackdrop = document.querySelector('.command-palette-backdrop.visible');
                if (paletteBackdrop) return; // let command-palette.js handle it
            }
            // (2) Close any visible modal-backdrop (but not the command palette backdrop)
            var modalBackdrop = document.querySelector('.modal-backdrop:not(.command-palette-backdrop)');
            if (modalBackdrop) {
                e.preventDefault();
                modalBackdrop.remove();
                return;
            }
            // (3) Exit focus mode if active
            if (document.documentElement.getAttribute('data-focus') === 'true') {
                e.preventDefault();
                toggleFocusMode(false);
                return;
            }
            // (4) Close code canvas if open
            if (window.CodeCanvas && document.querySelector('.code-canvas-panel.code-canvas-panel--open')) {
                e.preventDefault();
                CodeCanvas.close();
                return;
            }
            // (5) Close context panel if open
            var contextPanel = document.getElementById('contextPanel');
            if (contextPanel && contextPanel.classList.contains('open')) {
                e.preventDefault();
                contextPanel.classList.remove('open');
                return;
            }
            return;
        }

        // ── All other shortcuts: skip when user is typing ──
        if (isTypingContext(e)) return;

        if (!ctrl) return;

        // ── Ctrl+? (Ctrl+Shift+/) — shortcut reference card ──
        if (e.shiftKey && e.key === '?') {
            e.preventDefault();
            showShortcutOverlay();
            return;
        }

        // ── Ctrl+N — New conversation ──
        if (!e.shiftKey && !e.altKey && e.key === 'n') {
            e.preventDefault();
            if (typeof VApp !== 'undefined') {
                VApp.switchView('prompt');
                // Trigger the new project button to clear chat / show intake flow
                var newProjectBtn = document.getElementById('panelNewProjectBtn');
                if (newProjectBtn) newProjectBtn.click();
            }
            return;
        }

        // ── Ctrl+Shift+E — Toggle context panel ──
        if (e.shiftKey && !e.altKey && (e.key === 'E' || e.key === 'e')) {
            e.preventDefault();
            var panel = document.getElementById('contextPanel');
            if (panel) panel.classList.toggle('open');
            return;
        }

        // ── Ctrl+/ — Focus chat input ──
        if (!e.shiftKey && !e.altKey && e.key === '/') {
            e.preventDefault();
            var chatInput = document.getElementById('chatInput');
            if (chatInput) chatInput.focus();
            return;
        }

        // ── Ctrl+Shift+T — Toggle theme ──
        if (e.shiftKey && !e.altKey && (e.key === 'T' || e.key === 't')) {
            e.preventDefault();
            if (typeof VApp !== 'undefined') VApp.toggleTheme();
            return;
        }

        // ── Ctrl+Shift+F — Toggle focus mode ──
        if (e.shiftKey && !e.altKey && (e.key === 'F' || e.key === 'f')) {
            e.preventDefault();
            toggleFocusMode();
            return;
        }
    });

    // ── Focus Mode ──────────────────────────────────────────────────────────

    /**
     * Toggle distraction-free focus mode.
     * Hides sidebar, header, and centers the chat area.
     */
    function toggleFocusMode(forceState) {
        var html = document.documentElement;
        var isActive = html.getAttribute('data-focus') === 'true';
        var newState = typeof forceState === 'boolean' ? forceState : !isActive;

        html.setAttribute('data-focus', String(newState));
        localStorage.setItem('focusModeEnabled', String(newState));

        // Show/hide the exit pill
        var exitPill = document.getElementById('focusExitPill');
        if (exitPill) exitPill.style.display = newState ? 'flex' : 'none';
    }

    // Restore focus mode on load
    if (localStorage.getItem('focusModeEnabled') === 'true') {
        toggleFocusMode(true);
    }

    // Exit pill click handler
    bind('focusExitPill', function() { toggleFocusMode(false); });

    // Expose for command palette
    window.toggleFocusMode = toggleFocusMode;

    // ── Voice Input ─────────────────────────────────────────────────────────

    (function initVoiceInput() {
        var SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
        var voiceBtn = document.getElementById('voiceInputBtn');
        if (!SpeechRecognition || !voiceBtn) {
            // Hide mic button if speech API unavailable
            if (voiceBtn) voiceBtn.style.display = 'none';
            return;
        }

        var recognition = new SpeechRecognition();
        recognition.continuous = true;
        recognition.interimResults = true;
        recognition.lang = 'en-US';

        var isListening = false;
        var silenceTimer = null;
        var SILENCE_TIMEOUT = 3000;

        voiceBtn.addEventListener('click', function() {
            if (isListening) {
                stopListening();
            } else {
                startListening();
            }
        });

        function startListening() {
            try {
                recognition.start();
                isListening = true;
                voiceBtn.classList.add('listening');
                voiceBtn.setAttribute('aria-label', 'Stop voice input');
                resetSilenceTimer();
            } catch (err) {
                // Already started or unavailable
            }
        }

        function stopListening() {
            recognition.stop();
            isListening = false;
            voiceBtn.classList.remove('listening');
            voiceBtn.setAttribute('aria-label', 'Start voice input');
            clearTimeout(silenceTimer);
        }

        function resetSilenceTimer() {
            clearTimeout(silenceTimer);
            silenceTimer = setTimeout(function() {
                if (isListening) stopListening();
            }, SILENCE_TIMEOUT);
        }

        recognition.onresult = function(event) {
            resetSilenceTimer();
            var chatInput = document.getElementById('chatInput');
            if (!chatInput) return;

            var transcript = '';
            for (var i = 0; i < event.results.length; i++) {
                transcript += event.results[i][0].transcript;
            }
            chatInput.value = transcript;
            // Trigger input event so textarea auto-resizes
            chatInput.dispatchEvent(new Event('input', { bubbles: true }));
        };

        recognition.onerror = function() {
            stopListening();
        };

        recognition.onend = function() {
            if (isListening) {
                // Automatically stopped — mark as not listening
                isListening = false;
                voiceBtn.classList.remove('listening');
                voiceBtn.setAttribute('aria-label', 'Start voice input');
            }
        };
    })();

    // ── Adaptive Interface Hints ────────────────────────────────────────────

    (function initAdaptiveHints() {
        var PATTERNS_KEY = 'userPatterns';

        function getPatterns() {
            try {
                return JSON.parse(localStorage.getItem(PATTERNS_KEY)) || {
                    viewCounts: {},
                    categoryHistory: [],
                    modelUsage: {},
                    directChats: 0
                };
            } catch (e) {
                return { viewCounts: {}, categoryHistory: [], modelUsage: {}, directChats: 0 };
            }
        }

        function savePatterns(p) {
            localStorage.setItem(PATTERNS_KEY, JSON.stringify(p));
        }

        // Track view switches
        document.addEventListener('vetinari:viewSwitch', function(e) {
            var viewName = e.detail && e.detail.view;
            if (!viewName) return;
            var p = getPatterns();
            p.viewCounts[viewName] = (p.viewCounts[viewName] || 0) + 1;
            savePatterns(p);
        });

        // Apply patterns on load
        function applyPatterns() {
            var p = getPatterns();

            // Apply favorite model badges
            if (p.modelUsage) {
                var topModel = null;
                var topCount = 0;
                for (var model in p.modelUsage) {
                    if (p.modelUsage[model] > topCount) {
                        topCount = p.modelUsage[model];
                        topModel = model;
                    }
                }
                if (topModel && topCount >= 3) {
                    // Store for use by model rendering functions
                    window._favoriteModel = topModel;
                }
            }

            // Auto-hide categories if user skips them often
            if (p.directChats >= 5) {
                var catSection = document.getElementById('categoryGrid');
                if (catSection) catSection.style.display = 'none';
            }
        }

        applyPatterns();

        // Expose for Settings reset
        window.resetUserPatterns = function() {
            localStorage.removeItem(PATTERNS_KEY);
            window._favoriteModel = null;
        };
    })();

    // ── Connection Status Indicator ─────────────────────────────────────────

    (function initConnectionStatus() {
        var STATUS_POLL_INTERVAL = 30000; // 30s
        var statusDot = document.getElementById('connectionStatusDot');
        var statusPanel = document.getElementById('connectionStatusPanel');
        if (!statusDot) return;

        var isExpanded = false;
        var pollTimer = null;
        var lastStatus = null;

        statusDot.addEventListener('click', function(e) {
            e.stopPropagation();
            isExpanded = !isExpanded;
            if (statusPanel) {
                statusPanel.style.display = isExpanded ? 'block' : 'none';
            }
        });

        // Close on outside click
        document.addEventListener('click', function() {
            if (isExpanded && statusPanel) {
                isExpanded = false;
                statusPanel.style.display = 'none';
            }
        });

        if (statusPanel) {
            statusPanel.addEventListener('click', function(e) { e.stopPropagation(); });
        }

        function updateStatus() {
            fetch('/api/v1/status')
                .then(function(res) { return res.json(); })
                .then(function(data) {
                    lastStatus = data;
                    window._serverConnected = true;
                    var state = 'connected'; // green — server running
                    statusDot.className = 'status-indicator status-indicator--' + state;
                    statusDot.setAttribute('aria-label', 'Server status: ' + state);

                    // Update expanded panel content
                    if (statusPanel) {
                        var modelName = data.active_model || data.model || 'None loaded';
                        var html = '<div class="status-panel__row"><span>Backend</span><span class="status-panel__val status-panel__val--ok">Connected</span></div>';
                        html += '<div class="status-panel__row"><span>Model</span><span class="status-panel__val">' + escapeHtml(modelName) + '</span></div>';
                        if (data.gpu_available) {
                            html += '<div class="status-panel__row"><span>GPU</span><span class="status-panel__val">' + (data.gpu_utilization || 0) + '%</span></div>';
                        }
                        html += '<div class="status-panel__row"><span>Session Tokens</span><span class="status-panel__val">' + (window._sessionTokenCount || 0) + '</span></div>';
                        statusPanel.innerHTML = html;
                    }
                })
                .catch(function() {
                    window._serverConnected = false;
                    statusDot.className = 'status-indicator status-indicator--error';
                    statusDot.setAttribute('aria-label', 'Server status: disconnected');
                    if (statusPanel) {
                        statusPanel.innerHTML = '<div class="status-panel__row"><span>Backend</span><span class="status-panel__val status-panel__val--err">Disconnected</span></div>';
                    }
                });
        }

        function escapeHtml(str) {
            var div = document.createElement('div');
            div.textContent = str;
            return div.innerHTML;
        }

        // Set processing state during active projects
        document.addEventListener('vetinari:projectRunning', function() {
            statusDot.className = 'status-indicator status-indicator--processing';
        });

        // Initial check + polling
        updateStatus();
        pollTimer = setInterval(updateStatus, STATUS_POLL_INTERVAL);
    })();

    // ── Register commands in command palette ────────────────────────────────
    if (window.CommandPalette) {
        CommandPalette.registerCommand('show-shortcuts', 'Keyboard Shortcuts', 'Ctrl+?', showShortcutOverlay, 'Help');
        CommandPalette.registerCommand('toggle-focus', 'Toggle Focus Mode', 'Ctrl+Shift+F', function() { toggleFocusMode(); }, 'View');
        CommandPalette.registerCommand('toggle-code-canvas', 'Close Code Canvas', '', function() { if (window.CodeCanvas) CodeCanvas.close(); }, 'View');
    }

})();
