// Vetinari Web UI - Main Application
// Enhanced UX Version with Responsive & Micro-interactions

(function() {
'use strict';

// ==================== CONSTANTS ====================

/** Maximum characters to display before truncating task output (US-081). */
var TRUNCATE_DISPLAY_LIMIT = 20000;

// ==================== LOCALE CONFIG & FORMATTING ====================

/** Application-level locale configuration. currencySymbol is user-configurable. */
var VConfig = {
    currencySymbol: '$',   // Configurable currency symbol for all cost displays
    locale: undefined      // undefined = use the browser's default locale
};

/** Locale-aware number formatting utilities. Replaces bare .toFixed() at display sites. */
var VFmt = {
    /**
     * Format a number with N decimal places using the browser locale.
     * @param {number|null} value
     * @param {number} decimals
     * @returns {string}
     */
    decimal: function(value, decimals) {
        if (value == null || isNaN(value)) return '—';
        return new Intl.NumberFormat(VConfig.locale, {
            minimumFractionDigits: decimals,
            maximumFractionDigits: decimals
        }).format(value);
    },
    /**
     * Format as percentage (0-100 scale) using the browser locale.
     * @param {number|null} value
     * @param {number} [decimals]
     * @returns {string}
     */
    pct: function(value, decimals) {
        if (value == null || isNaN(value)) return '—';
        return new Intl.NumberFormat(VConfig.locale, {
            minimumFractionDigits: decimals || 0,
            maximumFractionDigits: decimals != null ? decimals : 1
        }).format(value) + '%';
    },
    /**
     * Format currency with the configured symbol using the browser locale.
     * @param {number|null} value
     * @param {number} [decimals]
     * @returns {string}
     */
    currency: function(value, decimals) {
        if (value == null || isNaN(value)) return '—';
        return VConfig.currencySymbol + new Intl.NumberFormat(VConfig.locale, {
            minimumFractionDigits: decimals != null ? decimals : 2,
            maximumFractionDigits: decimals != null ? decimals : 4
        }).format(value);
    }
};

// ==================== CONNECTION STATE ====================

/** Track whether the backend server is reachable to suppress noisy polling errors. */
window._serverConnected = true;
window._serverConnectedLastLog = 0;

// ==================== UTILITY FUNCTIONS ====================

/**
 * Debounce: delay execution until after `wait` ms since last call.
 */
function debounce(fn, wait) {
    let timer;
    return function(...args) {
        clearTimeout(timer);
        timer = setTimeout(() => fn.apply(this, args), wait);
    };
}

/**
 * loadCurrentProject: refresh the currently open project if one is active.
 */
function loadCurrentProject() {
    if (typeof currentProjectId !== 'undefined' && currentProjectId) {
        loadProjectDetails(currentProjectId);
    }
}


// ==================== RESPONSIVE SIDEBAR MANAGEMENT ====================

class SidebarManager {
    constructor() {
        this.sidebar = document.querySelector('.sidebar');
        this.mainContent = document.querySelector('.main-content');
        this.toggleBtn = document.getElementById('sidebarToggle');
        this.isCollapsed = this.loadCollapsedState();
        this.isMobile = window.innerWidth < 768;

        this.init();
    }

    init() {
        this.applyCollapsedState();
        this.attachEventListeners();
        this.handleResponsive();
    }

    loadCollapsedState() {
        return localStorage.getItem('sidebarCollapsed') === 'true';
    }

    saveCollapsedState(state) {
        localStorage.setItem('sidebarCollapsed', state);
    }

    applyCollapsedState() {
        if (this.isCollapsed && !this.isMobile) {
            this.sidebar.classList.add('collapsed');
            this.mainContent.classList.add('sidebar-collapsed');
        } else if (this.isMobile) {
            this.sidebar.classList.remove('open');
        }
    }

    toggle() {
        if (this.isMobile) {
            this.sidebar.classList.toggle('open');
            const isOpen = this.sidebar.classList.contains('open');
            this.toggleBtn.setAttribute('aria-expanded', isOpen);
        } else {
            this.isCollapsed = !this.isCollapsed;
            this.sidebar.classList.toggle('collapsed');
            this.mainContent.classList.toggle('sidebar-collapsed');
            this.saveCollapsedState(this.isCollapsed);
            this.toggleBtn.setAttribute('aria-expanded', !this.isCollapsed);
        }
    }

    attachEventListeners() {
        this.toggleBtn.addEventListener('click', () => this.toggle());
        document.addEventListener('click', (e) => {
            if (this.isMobile && !e.target.closest('.sidebar') && !e.target.closest('#sidebarToggle')) {
                this.sidebar.classList.remove('open');
            }
        });
    }

    handleResponsive() {
        window.addEventListener('resize', () => {
            const wasMobile = this.isMobile;
            this.isMobile = window.innerWidth < 768;

            if (wasMobile && !this.isMobile) {
                this.sidebar.classList.remove('open');
                if (this.isCollapsed) {
                    this.sidebar.classList.add('collapsed');
                    this.mainContent.classList.add('sidebar-collapsed');
                }
            } else if (!wasMobile && this.isMobile) {
                this.sidebar.classList.remove('collapsed');
                this.mainContent.classList.remove('sidebar-collapsed');
                this.sidebar.classList.remove('open');
            }
        });
    }
}

// Initialize sidebar manager
let sidebarManager;
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
        sidebarManager = new SidebarManager();
    });
} else {
    sidebarManager = new SidebarManager();
}

// ==================== ACCESSIBILITY ENHANCEMENTS ====================

class AccessibilityManager {
    constructor() {
        this.prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
        this.init();
    }

    init() {
        this.enhanceKeyboardNavigation();
        this.setupReducedMotionToggle();
    }

    enhanceKeyboardNavigation() {
        // Add keyboard navigation for sidebar
        document.addEventListener('keydown', (e) => {
            // Ctrl/Cmd + B to toggle sidebar
            if ((e.ctrlKey || e.metaKey) && e.key === 'b') {
                e.preventDefault();
                sidebarManager?.toggle();
            }
            // Escape to close mobile sidebar
            if (e.key === 'Escape' && sidebarManager?.isMobile) {
                document.querySelector('.sidebar')?.classList.remove('open');
            }
        });

        // Ensure all interactive elements are keyboard accessible
        const interactiveElements = document.querySelectorAll('.nav-item, .btn, input, textarea, select');
        interactiveElements.forEach(el => {
            if (!el.hasAttribute('tabindex') && el.tagName !== 'BUTTON' && el.tagName !== 'INPUT' && el.tagName !== 'TEXTAREA' && el.tagName !== 'SELECT') {
                el.setAttribute('tabindex', '0');
            }
        });
    }

    setupReducedMotionToggle() {
        if (this.prefersReducedMotion) {
            document.documentElement.style.setProperty('--transition-fast', '0ms');
            document.documentElement.style.setProperty('--transition-base', '0ms');
            document.documentElement.style.setProperty('--transition-slow', '0ms');
            document.documentElement.style.setProperty('--transition-slowest', '0ms');
        }
    }

    /**
     * Trap focus within an element (for modals).
     * Returns a cleanup function to remove the trap.
     *
     * @param {HTMLElement} element - The container element to trap focus within.
     * @returns {Function} Cleanup function that removes the focus trap.
     */
    static trapFocus(element) {
        var focusableSelectors = 'a[href], button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';
        var focusableEls = element.querySelectorAll(focusableSelectors);
        var firstEl = focusableEls[0];
        var lastEl = focusableEls[focusableEls.length - 1];
        var previouslyFocused = document.activeElement;

        function trapHandler(e) {
            if (e.key !== 'Tab') return;
            if (e.shiftKey) {
                if (document.activeElement === firstEl) {
                    e.preventDefault();
                    lastEl.focus();
                }
            } else {
                if (document.activeElement === lastEl) {
                    e.preventDefault();
                    firstEl.focus();
                }
            }
        }

        element.addEventListener('keydown', trapHandler);
        if (firstEl) firstEl.focus();

        return function() {
            element.removeEventListener('keydown', trapHandler);
            if (previouslyFocused && typeof previouslyFocused.focus === 'function') {
                previouslyFocused.focus();
            }
        };
    }
}

// Initialize accessibility manager
let a11yManager;
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
        a11yManager = new AccessibilityManager();
    });
} else {
    a11yManager = new AccessibilityManager();
}

// ==================== KEYBOARD SHORTCUTS ====================

// Keyboard shortcuts are handled by event-bindings.js (loaded after app.js).
// See event-bindings.js for: Ctrl+N, Ctrl+/, Ctrl+?, Ctrl+Shift+E,
// Ctrl+Shift+T, Escape priority chain. Ctrl+K and Ctrl+1-6 are in
// command-palette.js. Ctrl+B is in AccessibilityManager above.

// ==================== PREFERENCES MANAGER ====================

class PreferencesManager {
    constructor() {
        this.reducedMotionToggle = document.getElementById('reducedMotionToggle');
        this.compactModeToggle = document.getElementById('compactModeToggle');

        if (this.reducedMotionToggle || this.compactModeToggle) {
            this.loadPreferences();
            this.attachEventListeners();
        }
    }

    loadPreferences() {
        const reducedMotion = localStorage.getItem('reducedMotion') === 'true';
        const compactMode = localStorage.getItem('compactMode') === 'true';

        if (this.reducedMotionToggle) {
            this.reducedMotionToggle.checked = reducedMotion;
        }
        if (this.compactModeToggle) {
            this.compactModeToggle.checked = compactMode;
        }

        this.applyPreferences(reducedMotion, compactMode);
    }

    applyPreferences(reducedMotion, compactMode) {
        const root = document.documentElement;

        if (reducedMotion) {
            root.style.setProperty('--transition-fast', '0ms');
            root.style.setProperty('--transition-base', '0ms');
            root.style.setProperty('--transition-slow', '0ms');
            root.style.setProperty('--transition-slowest', '0ms');
        } else {
            root.style.removeProperty('--transition-fast');
            root.style.removeProperty('--transition-base');
            root.style.removeProperty('--transition-slow');
            root.style.removeProperty('--transition-slowest');
        }

        if (compactMode) {
            document.body.classList.add('compact-mode');
        } else {
            document.body.classList.remove('compact-mode');
        }
    }

    attachEventListeners() {
        if (this.reducedMotionToggle) {
            this.reducedMotionToggle.addEventListener('change', (e) => {
                localStorage.setItem('reducedMotion', e.target.checked);
                this.applyPreferences(e.target.checked, document.getElementById('compactModeToggle')?.checked || false);
            });
        }

        if (this.compactModeToggle) {
            this.compactModeToggle.addEventListener('change', (e) => {
                localStorage.setItem('compactMode', e.target.checked);
                this.applyPreferences(document.getElementById('reducedMotionToggle')?.checked || false, e.target.checked);
            });
        }
    }
}

// Initialize preferences manager
let prefsManager;
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
        prefsManager = new PreferencesManager();
    });
} else {
    prefsManager = new PreferencesManager();
}

// Safe JSON parsing helper — checks response.ok before attempting JSON parse
function safeJsonParse(response) {
    if (!response.ok) {
        return response.json().catch(() => ({
            error: `Request failed (${response.status})`,
            status: response.status
        })).then(data => {
            if (data && !data.error) data.error = `Request failed (${response.status})`;
            return data;
        });
    }
    return response.json().catch(err => {
        console.error('JSON parse error:', err);
        return { error: 'Invalid JSON response', details: err.message };
    });
}

/**
 * Consistent API call wrapper with error handling and toast notifications.
 * All new code MUST use this. Returns parsed JSON on success, null on failure.
 */
async function apiCall(url, options = {}) {
    try {
        const res = await fetch(url, {
            headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest', ...options.headers },
            ...options
        });
        const data = await safeJsonParse(res);
        if (!res.ok || data.error) {
            if (window.ToastManager) {
                ToastManager.show(data.error || `Request failed (${res.status})`, 'error');
            } else {
                showStatusBanner(data.error || `Request failed (${res.status})`, 'error');
            }
            return null;
        }
        return data;
    } catch (err) {
        if (window.ToastManager) {
            ToastManager.show('Network error — check your connection', 'error');
        } else {
            showStatusBanner('Network error', 'error');
        }
        console.error('API call failed:', url, err);
        return null;
    }
}

// Theme management
function getSystemThemePreference() {
    return window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
}

function initTheme() {
    const savedTheme = localStorage.getItem('theme');
    if (savedTheme === 'light' || savedTheme === 'dark') {
        document.documentElement.setAttribute('data-theme', savedTheme);
        updateThemeIcon(savedTheme);
        return;
    }
    document.documentElement.removeAttribute('data-theme');
    updateThemeIcon(getSystemThemePreference());
}

function toggleTheme() {
    const current = document.documentElement.getAttribute('data-theme') || getSystemThemePreference();
    const newTheme = current === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', newTheme);
    localStorage.setItem('theme', newTheme);
    updateThemeIcon(newTheme);
}

function updateThemeIcon(theme) {
    const btn = document.getElementById('themeToggle');
    if (btn) {
        const icon = btn.querySelector('i');
        if (icon) {
            icon.className = theme === 'dark' ? 'fas fa-moon' : 'fas fa-sun';
        }
    }
}

/**
 * Show keyboard shortcut help overlay.
 */
function showShortcutHelp() {
    var shortcuts = [
        { key: 'Ctrl+Enter', desc: 'Send message' },
        { key: 'Ctrl+K', desc: 'Command palette' },
        { key: 'Ctrl+/', desc: 'Toggle sidebar' },
        { key: 'Escape', desc: 'Close modal / cancel' },
    ];
    var html = '<div style="padding:1rem;"><h3 style="margin-bottom:1rem;">Keyboard Shortcuts</h3>';
    shortcuts.forEach(function(s) {
        html += '<div style="display:flex;justify-content:space-between;padding:0.5rem 0;border-bottom:1px solid var(--border-default);">' +
            '<kbd style="background:var(--dark-tertiary);padding:2px 8px;border-radius:4px;font-family:monospace;">' + s.key + '</kbd>' +
            '<span>' + s.desc + '</span></div>';
    });
    html += '</div>';
    if (window.ToastManager) {
        ToastManager.show('Press Ctrl+K for command palette', 'info');
    }
}

/**
 * Apply interface mode (simple/standard/expert) from server preferences.
 * Sets data-mode attribute on the document element which drives CSS visibility rules.
 */
function applyInterfaceMode(mode) {
    const validModes = ['simple', 'standard', 'expert'];
    const resolvedMode = validModes.includes(mode) ? mode : 'standard';
    document.documentElement.setAttribute('data-mode', resolvedMode);
}

// State
let currentView = 'prompt';
let models = [];
let tasks = [];
let activityLog = [];
let sidebarCollapsed = false;

// SSE (Server-Sent Events) management via SSEManager

function subscribeToProjectStream(projectId) {
    if (window.SSEManager && SSEManager.isConnected(`project-${projectId}`)) return;

    const url = `/api/project/${encodeURIComponent(projectId)}/stream`;

    const eventHandlers = {
        task_started(e) {
            var data;
            try { data = JSON.parse(e.data); } catch (_pe) { console.warn('SSE parse error:', _pe.message); return; }
            // Execution narrative: show agent/model/description
            const narrative = data.narrative || `Task ${data.task_id} started (${data.model})`;
            addActivity(narrative, 'info');
            updateTaskStatusInUI(projectId, data.task_id, 'running');
            document.dispatchEvent(new CustomEvent(`sse:task_started:${projectId}`, { detail: data }));
        },
        task_completed(e) {
            var data;
            try { data = JSON.parse(e.data); } catch (_pe) { console.warn('SSE parse error:', _pe.message); return; }
            const pct = Math.round(((data.task_index + 1) / data.total) * 100);
            addActivity(`Task ${data.task_id} complete — ${data.tokens_used || 0} tokens`, 'success');
            updateTaskStatusInUI(projectId, data.task_id, 'completed');
            updateProgressBar(pct);
            updateTokenCounter(projectId, data.tokens_used || 0);
            // Auto-open CodeCanvas if task output contains multi-file code blocks
            if (data.output) {
                maybeOpenCodeCanvas(data.output);
            }
            document.dispatchEvent(new CustomEvent(`sse:task_completed:${projectId}`, { detail: data }));
        },
        task_failed(e) {
            var data;
            try { data = JSON.parse(e.data); } catch (_pe) { console.warn('SSE parse error:', _pe.message); return; }
            addActivity(`Task ${data.task_id} failed`, 'error');
            updateTaskStatusInUI(projectId, data.task_id, 'failed');
            document.dispatchEvent(new CustomEvent(`sse:task_failed:${projectId}`, { detail: data }));
        },
        task_cancelled(e) {
            var data;
            try { data = JSON.parse(e.data); } catch (_pe) { console.warn('SSE parse error:', _pe.message); return; }
            addActivity(`Task ${data.task_id} cancelled`, 'warning');
            updateTaskStatusInUI(projectId, data.task_id, 'cancelled');
            document.dispatchEvent(new CustomEvent(`sse:task_cancelled:${projectId}`, { detail: data }));
        },
        status(e) {
            var data;
            try { data = JSON.parse(e.data); } catch (_pe) { console.warn('SSE parse error:', _pe.message); return; }
            if (data.status === 'completed') {
                addActivity('All tasks completed!', 'success');
                showStatusBanner('Project completed successfully!', 'success');
                loadProjectDetails(projectId);
                unsubscribeFromProjectStream(projectId);
                const ps = document.getElementById('progressSection');
                if (ps) ps.style.display = 'none';
                // Hide execution control buttons
                var cancelBtn = document.getElementById('cancelProjectBtn');
                var pauseBtn = document.getElementById('pauseProjectBtn');
                var resumeBtn = document.getElementById('resumeProjectBtn');
                if (cancelBtn) cancelBtn.style.display = 'none';
                if (pauseBtn) pauseBtn.style.display = 'none';
                if (resumeBtn) resumeBtn.style.display = 'none';
            }
        },
        cancelled() {
            addActivity('Project cancelled by user', 'warning');
            showStatusBanner('Project cancelled', 'warning');
            unsubscribeFromProjectStream(projectId);
            const ps = document.getElementById('progressSection');
            if (ps) ps.style.display = 'none';
            // Hide execution control buttons
            var cancelBtn = document.getElementById('cancelProjectBtn');
            var pauseBtn = document.getElementById('pauseProjectBtn');
            var resumeBtn = document.getElementById('resumeProjectBtn');
            if (cancelBtn) cancelBtn.style.display = 'none';
            if (pauseBtn) pauseBtn.style.display = 'none';
            if (resumeBtn) resumeBtn.style.display = 'none';
        },
        paused() {
            addActivity('Project paused', 'info');
            showStatusBanner('Project paused — click resume to continue', 'info');
            var pauseBtn = document.getElementById('pauseProjectBtn');
            var resumeBtn = document.getElementById('resumeProjectBtn');
            if (pauseBtn) pauseBtn.style.display = 'none';
            if (resumeBtn) resumeBtn.style.display = '';
        },
        resumed() {
            addActivity('Project resumed', 'success');
            showStatusBanner('Project resumed', 'success');
            var pauseBtn = document.getElementById('pauseProjectBtn');
            var resumeBtn = document.getElementById('resumeProjectBtn');
            if (pauseBtn) pauseBtn.style.display = '';
            if (resumeBtn) resumeBtn.style.display = 'none';
        },
        task_rerun(e) {
            var data;
            try { data = JSON.parse(e.data); } catch (_pe) { console.warn('SSE parse error:', _pe.message); return; }
            addActivity('Task ' + data.task_id + ' queued for re-run', 'info');
        },
        model_recommendation(e) {
            var data;
            try { data = JSON.parse(e.data); } catch (_pe) { console.warn('SSE parse error:', _pe.message); return; }
            if (window.ToastManager) {
                ToastManager.showRecommendation(
                    data.model_name || data.model,
                    data.reason || 'A better model is available for this task',
                    function() {
                        // Navigate to Models > Discover tab
                        if (typeof switchView === 'function') switchView('models');
                        setTimeout(function() {
                            var discoverTab = document.querySelector('[data-tab="models-discover"]');
                            if (discoverTab) discoverTab.click();
                        }, 100);
                    }
                );
            }
        },
        stage_started(e) {
            var data;
            try { data = JSON.parse(e.data); } catch (_pe) { console.warn('SSE parse error:', _pe.message); return; }
            // Map pipeline stage to agent node
            const agentMap = { planning: 'foreman', foreman: 'foreman', building: 'worker', worker: 'worker', review: 'inspector', inspector: 'inspector' };
            const agent = agentMap[data.stage] || data.stage;
            if (window.ActivityTracker) {
                ActivityTracker.startActivity('stage-' + data.stage, data.label || data.stage, { step: data.step, total: data.total_steps });
                ActivityTracker.updatePipelineNode(agent, 'active', { mode: data.label || data.stage });
            }
            document.dispatchEvent(new CustomEvent('vetinari:projectRunning'));
            // Show execution control buttons
            var cancelBtn = document.getElementById('cancelProjectBtn');
            var pauseBtn = document.getElementById('pauseProjectBtn');
            if (cancelBtn) cancelBtn.style.display = '';
            if (pauseBtn) pauseBtn.style.display = '';
        },
        stage_completed(e) {
            var data;
            try { data = JSON.parse(e.data); } catch (_pe) { console.warn('SSE parse error:', _pe.message); return; }
            const agentMap = { planning: 'foreman', foreman: 'foreman', building: 'worker', worker: 'worker', review: 'inspector', inspector: 'inspector' };
            const agent = agentMap[data.stage] || data.stage;
            if (window.ActivityTracker) {
                ActivityTracker.completeActivity('stage-' + data.stage, 'Completed in ' + VFmt.decimal(data.duration_ms / 1000, 1) + 's');
                ActivityTracker.updatePipelineNode(agent, 'completed', { time: VFmt.decimal(data.duration_ms / 1000, 1) + 's' });
            }
        },
        stage_progress(e) {
            var data;
            try { data = JSON.parse(e.data); } catch (_pe) { console.warn('SSE parse error:', _pe.message); return; }
            if (window.ActivityTracker) {
                const pct = data.sub_total ? Math.round((data.sub_step / data.sub_total) * 100) : null;
                ActivityTracker.updateProgress('stage-' + data.stage, pct, data.detail);
            }
        },
        eta_update(e) {
            var data;
            try { data = JSON.parse(e.data); } catch (_pe) { console.warn('SSE parse error:', _pe.message); return; }
            if (window.ActivityTracker) {
                // Update ETA on the most recent active stage
                var activeActivities = ActivityTracker.getActiveActivities();
                if (activeActivities.length > 0) {
                    ActivityTracker.updateETA(activeActivities[activeActivities.length - 1].id, data.estimated_remaining_seconds);
                }
            }
        },
        thinking(e) {
            var data;
            try { data = JSON.parse(e.data); } catch (_pe) { console.warn('SSE parse error:', _pe.message); return; }
            // Append thinking content to collapsible details in chat
            const thinkingId = 'thinking-' + (data.task_id || 'current');
            let details = document.getElementById(thinkingId);
            if (!details) {
                const chatArea = document.getElementById('chatMessages');
                if (chatArea) {
                    details = document.createElement('details');
                    details.id = thinkingId;
                    details.className = 'thinking-trace';
                    details.innerHTML = '<summary>Reasoning...</summary><pre class="thinking-content"></pre>';
                    chatArea.appendChild(details);
                }
            }
            if (details) {
                const pre = details.querySelector('.thinking-content');
                if (pre) pre.textContent += data.content;
            }
        },
        decision(e) {
            var data;
            try { data = JSON.parse(e.data); } catch (_pe) { console.warn('SSE parse error:', _pe.message); return; }
            // Show toast for auto-change decisions (US-024)
            const typeLabels = {
                model_selection: 'Model selected',
                model_swap: 'Model changed',
                retry_action: 'Retry decision',
                quality_gate: 'Quality gate',
                cascade_escalation: 'Escalated',
                tier_classification: 'Classified'
            };
            const label = typeLabels[data.decision_type] || data.decision_type;
            const msg = label + ': ' + (data.choice || '');
            if (window.ToastManager) {
                ToastManager.show(msg, 'info', { duration: 6000 });
            }
            addActivity(msg + ' — ' + (data.reasoning || ''), 'info');

            // Append decision to chat as inline narrative (US-025)
            const chatArea = document.getElementById('chatMessages');
            if (chatArea) {
                const decEl = document.createElement('div');
                decEl.className = 'chat-decision-inline';
                decEl.innerHTML = '<i class="fas fa-gavel"></i> <strong>' + VApp.escapeHtml(label)
                    + '</strong>: ' + VApp.escapeHtml(data.choice || '')
                    + (data.reasoning ? ' <span class="decision-reason">(' + VApp.escapeHtml(data.reasoning) + ')</span>' : '');
                chatArea.appendChild(decEl);
                chatArea.scrollTop = chatArea.scrollHeight;
            }
        }
    };

    if (window.SSEManager) {
        SSEManager.connect(`project-${projectId}`, url, eventHandlers);
    } else {
        // Fallback if SSEManager not loaded
        const es = new EventSource(url);
        for (const [event, handler] of Object.entries(eventHandlers)) {
            es.addEventListener(event, handler);
        }
        es.onerror = () => {
            if (es.readyState === EventSource.CLOSED) es.close();
        };
        window._sseFallback = window._sseFallback || {};
        window._sseFallback[projectId] = es;
    }
}

function unsubscribeFromProjectStream(projectId) {
    if (window.SSEManager) {
        SSEManager.close(`project-${projectId}`);
    } else if (window._sseFallback && window._sseFallback[projectId]) {
        window._sseFallback[projectId].close();
        delete window._sseFallback[projectId];
    }
}

// Token counter display
let _sessionTokens = 0;

function updateTokenCounter(projectId, tokensAdded) {
    _sessionTokens += tokensAdded;
    const el = document.querySelector('#tokenCounter span:last-child');
    if (el) el.textContent = `${_sessionTokens.toLocaleString()} tokens`;
}

async function loadTokenStats() {
    try {
        const res = await fetch('/api/v1/token-stats');
        if (!res.ok) return;
        const data = await res.json();
        _sessionTokens = data.total_tokens_used || 0;
        const el = document.querySelector('#tokenCounter span:last-child');
        if (el) el.textContent = `${_sessionTokens.toLocaleString()} tokens`;
    } catch (e) { /* silently ignore */ }
}

// Task status update helper
function updateTaskStatusInUI(projectId, taskId, status) {
    const el = document.querySelector(`[data-task-id="${taskId}"] .task-status`);
    if (el) {
        el.className = `task-status status-${status}`;
        el.textContent = status;
    }
}

// Progress bar update
function updateProgressBar(pct) {
    // 'overallProgressBar' matches the element ID in index.html
    const bar = document.getElementById('overallProgressBar') || document.getElementById('progressFill');
    const label = document.getElementById('progressPercent');
    if (bar) bar.style.width = `${pct}%`;
    if (label) label.textContent = `${pct}%`;
}

// Cancel current project (called from HTML with no arguments)
function cancelCurrentProject() {
    if (typeof currentProjectId !== 'undefined' && currentProjectId) {
        cancelProject(currentProjectId);
    }
}

async function cancelProject(projectId) {
    if (!confirm('Cancel the running project? This will stop execution.')) return;
    try {
        const res = await fetch(`/api/project/${encodeURIComponent(projectId)}/cancel`, {method: 'POST', headers: {'X-Requested-With': 'XMLHttpRequest'}});
        if (!res.ok) { console.warn('Cancel failed:', res.status); return; }
        const data = await res.json();
        if (data.status === 'cancelled') {
            showStatusBanner('Project cancelled', 'warning');
            unsubscribeFromProjectStream(projectId);
            loadProjectDetails(projectId);
        }
    } catch (e) {
        addActivity('Failed to cancel project: ' + e.message, 'error');
    }
}

// Global search results overlay
function showSearchResults(results, query) {
    let overlay = document.getElementById('searchResultsOverlay');
    if (!overlay) {
        overlay = document.createElement('div');
        overlay.id = 'searchResultsOverlay';
        overlay.style.cssText = 'position:fixed;top:60px;left:50%;transform:translateX(-50%);width:600px;max-width:90vw;background:var(--surface-bg);border:1px solid var(--border-color);border-radius:12px;box-shadow:var(--shadow-lg);z-index:9999;max-height:400px;overflow-y:auto;padding:1rem;';
        document.body.appendChild(overlay);
        document.addEventListener('click', (e) => {
            if (!overlay.contains(e.target) && e.target.id !== 'globalSearch') hideSearchResults();
        });
    }

    if (!results || results.length === 0) {
        overlay.innerHTML = `<p style="color:var(--text-muted);text-align:center;padding:1rem;">No results for "<strong>${escapeHtml(query)}</strong>"</p>`;
    } else {
        overlay.innerHTML = `
            <div style="font-size:0.75rem;color:var(--text-muted);margin-bottom:0.5rem;">${results.length} result(s) for "<strong>${escapeHtml(query)}</strong>"</div>
            ${results.map(r => `
                <div class="search-result-item" data-action="searchResultClick" data-id="${escapeHtml(r.type === 'project' ? r.id : (r.project_id || ''))}" style="padding:0.5rem;cursor:pointer;border-radius:6px;margin-bottom:0.25rem;border:1px solid var(--border-subtle);">
                    <div style="display:flex;gap:0.5rem;align-items:center;">
                        <span class="badge" style="font-size:0.7rem;background:var(--primary-muted);color:var(--primary-color);padding:2px 6px;border-radius:4px;">${escapeHtml(r.type)}</span>
                        <strong>${escapeHtml(r.name || r.task_id || 'Result')}</strong>
                    </div>
                    ${r.description ? `<div style="font-size:0.8rem;color:var(--text-muted);margin-top:2px;">${escapeHtml(r.description)}</div>` : ''}
                    ${r.preview ? `<div style="font-size:0.8rem;color:var(--text-muted);margin-top:2px;font-style:italic;">${escapeHtml(r.preview)}</div>` : ''}
                </div>
            `).join('')}
        `;
    }
    overlay.style.display = 'block';
}

function hideSearchResults() {
    const overlay = document.getElementById('searchResultsOverlay');
    if (overlay) overlay.style.display = 'none';
}

// Status Banner Functions
function showStatusBanner(message, type = 'info') {
    const banner = document.getElementById('statusBanner');
    const text = document.getElementById('statusBannerText');
    const content = document.getElementById('statusBannerContent');

    if (banner && text && content) {
        banner.style.display = '';  // clear inline display:none so CSS .visible class works
        banner.className = 'status-banner visible ' + type;
        text.textContent = message;

        // Auto-hide after 5 seconds for info
        if (type === 'info') {
            setTimeout(() => {
                hideStatusBanner();
            }, 5000);
        }
    }
}

function hideStatusBanner() {
    const banner = document.getElementById('statusBanner');
    if (banner) {
        banner.className = 'status-banner';
        banner.style.display = 'none';
    }
}

window.hideStatusBanner = hideStatusBanner;

// Initialize - everything inside DOMContentLoaded
document.addEventListener('DOMContentLoaded', () => {
    initTheme();
    initNavigation();
    // Sidebar already initialized by SidebarManager (class at top of file)
    initAdvancedNav();

    // Apply interface mode from server preferences and sync setup state
    fetch('/api/v1/preferences').then(r => r.json()).then(data => {
        var prefs = (data && data.preferences) ? data.preferences : data;
        if (prefs && prefs.interfaceMode) {
            applyInterfaceMode(prefs.interfaceMode);
        }
        // Sync server-side setupComplete to localStorage so wizard respects it
        if (prefs && prefs.setupComplete && window.VStore) {
            VStore.set(VStore.KEYS.SETUP_COMPLETE, true);
        }
        // Show wizard only if setup truly not complete (after server sync)
        if (window.SetupWizard && window.VStore && !VStore.get(VStore.KEYS.SETUP_COMPLETE)) {
            new SetupWizard().show();
        }
    }).catch(() => {
        // Fallback: check localStorage only
        if (window.SetupWizard && window.VStore && !VStore.get(VStore.KEYS.SETUP_COMPLETE)) {
            new SetupWizard().show();
        }
    });

    // Load chat (prompt) as the default view, and dashboard data in background
    // for model count and local inference status
    loadPrompt();
    loadDashboard().then(() => checkLocalInferenceStatus());
    loadSettings();
    loadSidebarProjects();
    loadTokenStats();

    // Auto-refresh — pause when tab is hidden (Page Visibility API)
    const _autoRefreshIntervals = [];
    function _startAutoRefresh() {
        _autoRefreshIntervals.push(setInterval(loadDashboard, 60000));
        _autoRefreshIntervals.push(setInterval(loadSidebarProjects, 15000));
        _autoRefreshIntervals.push(setInterval(checkLocalInferenceStatus, 90000));
        _autoRefreshIntervals.push(setInterval(loadTokenStats, 30000));
    }
    function _stopAutoRefresh() {
        _autoRefreshIntervals.forEach(id => clearInterval(id));
        _autoRefreshIntervals.length = 0;
    }
    _startAutoRefresh();
    document.addEventListener('visibilitychange', () => {
        if (document.hidden) {
            _stopAutoRefresh();
        } else {
            _startAutoRefresh();
        }
    });
    window.addEventListener('beforeunload', _stopAutoRefresh);

    // Global search
    const searchInput = document.getElementById('globalSearch');
    if (searchInput) {
        const doSearch = debounce(async () => {
            const q = searchInput.value.trim();
            if (q.length < 2) return;
            try {
                const res = await fetch(`/api/v1/search?q=${encodeURIComponent(q)}`);
                const data = await res.json();
                showSearchResults(data.results, q);
            } catch (e) { /* ignore */ }
        }, 400);
        searchInput.addEventListener('input', doSearch);
        searchInput.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                searchInput.value = '';
                hideSearchResults();
            }
        });
    }

    // Output dropdown change handler
    document.getElementById('outputTaskSelect')?.addEventListener('change', loadOutputForTask);

    // Model swap on prompt page
    document.getElementById('chatModelSelect')?.addEventListener('change', async (e) => {
        const modelId = e.target.value;
        if (!modelId || !currentProjectId) return;

        try {
            const res = await fetch('/api/v1/swap-model', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
                body: JSON.stringify({ project_id: currentProjectId, model_id: modelId })
            });
            const data = await safeJsonParse(res);
            if (data.status === 'swapped') {
                addActivity(`Swapped to model: ${modelId}`);
            }
        } catch (error) {
            console.error('Error swapping model:', error);
        }
    });

    // Archive filters
    document.getElementById('archiveSearch')?.addEventListener('input', loadArchive);
    document.getElementById('archiveStatusFilter')?.addEventListener('change', loadArchive);

    // Theme toggle
    document.getElementById('themeToggle')?.addEventListener('click', toggleTheme);

    // Header buttons
    document.getElementById('discoverBtn')?.addEventListener('click', async () => {
        const btn = document.getElementById('discoverBtn');
        if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i>'; }
        addActivity('Discovering models...');
        try {
            // Force-refresh bypasses the TTL cache
            const res = await fetch('/api/v1/models/refresh', { method: 'POST', headers: {'X-Requested-With': 'XMLHttpRequest'} });
            const data = await safeJsonParse(res);
            if (data.error) {
                addActivity('Discovery error: ' + data.error, 'error');
            } else {
                addActivity(`Found ${data.count || data.discovered || 0} models`);
                _lastModelCount = data.count || 0;
                updateLMStatusBar(true, _lastModelCount, '');
            }
            loadModels();
            loadDashboard();
        } catch (error) {
            addActivity('Error discovering models', 'error');
        } finally {
            if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fas fa-compass"></i>'; }
        }
    });

    document.getElementById('runAllBtn')?.addEventListener('click', async () => {
        const userGoal = window.prompt('Enter your goal for the project:');
        if (!userGoal) return;

        addActivity(`Creating project with goal: ${userGoal.substring(0, 50)}...`);
        showStatusBanner('Creating project and planning tasks...', 'info');

        const btn = document.getElementById('runAllBtn');
        if (btn) {
            btn.disabled = true;
            btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Creating...';
        }

        try {
            const res = await fetch('/api/new-project', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
                body: JSON.stringify({ goal: userGoal, auto_run: true })
            });
            const data = await safeJsonParse(res);

            if (data.error) {
                addActivity('Error creating project: ' + data.error, 'error');
                showStatusBanner('Error: ' + data.error, 'error');
            } else {
                addActivity(`Project created: ${data.project_id} with ${data.tasks?.length || 0} tasks`);

                // Show warnings if any
                if (data.warnings && data.warnings.length > 0) {
                    showStatusBanner(data.warnings[0], 'warning');
                } else {
                    showStatusBanner('Project created! Tasks are running.', 'success');
                }

                // Show the plan
                if (data.tasks && data.tasks.length > 0) {
                    let planMsg = 'Tasks:\n';
                    data.tasks.forEach(t => {
                        planMsg += `- ${t.id}: ${t.description} (${t.assigned_model_id})\n`;
                    });
                    addActivity(planMsg);
                }

                // Refresh projects
                loadSidebarProjects();

                // Subscribe to SSE stream for real-time updates
                if (data.project_id) {
                    subscribeToProjectStream(data.project_id);
                    currentProjectId = data.project_id;
                }

                // Switch to workflow view to show progress
                switchView('workflow');
            }
            loadDashboard();
        } catch (error) {
            addActivity('Error creating project: ' + error, 'error');
        } finally {
            if (btn) {
                btn.disabled = false;
                btn.innerHTML = '<i class="fas fa-play"></i> Run All Tasks';
            }
        }
    });

    // Model view buttons
    document.getElementById('refreshModels')?.addEventListener('click', loadModels);

    // Workflow view buttons
    document.getElementById('refreshWorkflow')?.addEventListener('click', loadWorkflow);

    // Chat buttons
    document.getElementById('sendMessageBtn')?.addEventListener('click', sendChatMessage);
    document.getElementById('newProjectBtn')?.addEventListener('click', () => {
        currentProjectId = null;
        document.getElementById('chatMessages').innerHTML = `
            <div class="chat-empty" id="chatEmpty">
                <i class="fas fa-comments"></i>
                <p>Choose a category above, or just type your goal below</p>
            </div>
        `;
        document.getElementById('chatTasks').style.display = 'none';
        document.querySelectorAll('.project-item').forEach(item => {
            item.classList.remove('active');
        });
        // Show intake flow category grid
        if (window.IntakeFlow) {
            window.IntakeFlow.show();
        }
        // Update chat input placeholder
        const chatInput = document.getElementById('chatInput');
        if (chatInput) {
            chatInput.placeholder = 'Describe your goal...';
            chatInput.focus();
        }
    });

    // Chat input enter key
    document.getElementById('chatInput')?.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && e.ctrlKey) {
            sendChatMessage();
        }
    });

    // Chat draft persistence — save to localStorage on every keystroke, restore on load
    const _chatDraftInput = document.getElementById('chatInput');
    if (_chatDraftInput) {
        const savedDraft = localStorage.getItem('vetinari_chat_draft');
        if (savedDraft) _chatDraftInput.value = savedDraft;
        _chatDraftInput.addEventListener('input', () => {
            const val = _chatDraftInput.value;
            if (val) {
                localStorage.setItem('vetinari_chat_draft', val);
            } else {
                localStorage.removeItem('vetinari_chat_draft');
            }
        });
    }

    // System prompt management
    document.getElementById('toggleSystemPromptBtn')?.addEventListener('click', () => {
        const panel = document.getElementById('systemPromptPanel');
        panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
    });

    document.getElementById('systemPromptSelect')?.addEventListener('change', (e) => {
        const input = document.getElementById('systemPromptInput');
        if (e.target.value) {
            input.value = e.target.value;
            currentSystemPrompt = e.target.value;
        } else {
            input.value = 'You are Vetinari, an AI orchestration assistant. Provide structured, actionable responses with clear reasoning.';
            currentSystemPrompt = '';
        }
    });

    document.getElementById('systemPromptInput')?.addEventListener('input', (e) => {
        currentSystemPrompt = e.target.value;
    });

    document.getElementById('newSystemPromptBtn')?.addEventListener('click', async () => {
        const name = prompt('Enter a name for this system prompt preset:');
        if (!name) return;

        const content = document.getElementById('systemPromptInput')?.value || 'You are Vetinari, an AI orchestration assistant. Provide structured, actionable responses with clear reasoning.';

        try {
            const res = await fetch('/api/v1/system-prompts', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
                body: JSON.stringify({ name, content })
            });
            const data = await safeJsonParse(res);

            if (data.status === 'saved') {
                addActivity('System prompt saved as: ' + name);
                loadPrompt();
            } else if (data.error) {
                addActivity('Error saving prompt: ' + data.error, 'error');
            }
        } catch (error) {
            addActivity('Error saving prompt', 'error');
        }
    });

    document.getElementById('deleteSystemPromptBtn')?.addEventListener('click', async () => {
        const select = document.getElementById('systemPromptSelect');
        const selectedOption = select?.selectedOptions[0];

        if (!selectedOption || !selectedOption.value) {
            addActivity('No preset selected to delete', 'error');
            return;
        }

        const name = selectedOption.text;
        if (!confirm(`Delete system prompt preset "${name}"?`)) return;

        try {
            const res = await fetch(`/api/v1/system-prompts/${encodeURIComponent(name)}`, {
                method: 'DELETE',
                headers: {'X-Requested-With': 'XMLHttpRequest'}
            });
            const data = await safeJsonParse(res);

            if (data.status === 'deleted') {
                addActivity('System prompt deleted');
                loadPrompt();
            } else if (data.error) {
                addActivity('Error deleting prompt: ' + data.error, 'error');
            }
        } catch (error) {
            addActivity('Error deleting prompt', 'error');
        }
    });
});

// Sidebar
function initSidebar() {
    const toggleBtn = document.getElementById('sidebarToggle');
    toggleBtn?.addEventListener('click', () => {
        sidebarCollapsed = !sidebarCollapsed;
        document.querySelector('.sidebar').classList.toggle('collapsed', sidebarCollapsed);
        document.querySelector('.main-content').classList.toggle('sidebar-collapsed', sidebarCollapsed);
    });
}

// Navigation
function initNavigation() {
    const navItems = document.querySelectorAll('.nav-item');
    navItems.forEach(item => {
        item.addEventListener('click', (e) => {
            e.preventDefault();
            const view = item.dataset.view;
            if (view) switchView(view);
        });
    });
}

// Handle browser back/forward via popstate
window.addEventListener('popstate', (e) => {
    if (e.state && e.state.view) {
        switchView(e.state.view);
    } else {
        // Parse view from URL path
        const path = window.location.pathname.replace(/^\//, '') || 'prompt';
        switchView(path);
    }
});

// On initial load, check if URL has a view path
(function _initViewFromUrl() {
    const path = window.location.pathname.replace(/^\//, '');
    if (path && path !== 'prompt') {
        // Replace the initial history entry so back-button works correctly
        history.replaceState({ view: path }, '', window.location.pathname);
    } else {
        history.replaceState({ view: 'prompt' }, '', '/');
    }
})();

// Advanced nav section toggle
function initAdvancedNav() {
    const toggle = document.getElementById('advancedNavToggle');
    const items = document.getElementById('advancedNavItems');
    const chevron = document.getElementById('advancedNavChevron');
    if (!toggle || !items) return;

    toggle.addEventListener('click', () => {
        const expanded = toggle.getAttribute('aria-expanded') === 'true';
        toggle.setAttribute('aria-expanded', !expanded);
        items.style.display = expanded ? 'none' : 'flex';
        items.style.flexDirection = 'column';
        items.style.gap = '4px';
        if (chevron) {
            chevron.style.transform = expanded ? '' : 'rotate(90deg)';
        }
    });

    // Auto-expand if an advanced view is active
    const activeAdvanced = items.querySelector('.nav-item.active');
    if (activeAdvanced) {
        toggle.setAttribute('aria-expanded', 'true');
        items.style.display = 'flex';
    }
}

// Local inference status — uses /api/status only (no extra discovery call)
async function checkLocalInferenceStatus() {
    try {
        const res = await fetch('/api/v1/status');
        const status = await safeJsonParse(res);
        // Re-use the model count from the dashboard data if available; otherwise
        // rely on whatever was last fetched. Avoids a duplicate /api/models call.
        const modelCount = _lastModelCount !== undefined ? _lastModelCount : 0;
        updateLMStatusBar(status.status === 'running', modelCount);
    } catch (e) {
        updateLMStatusBar(false, 0);
    }
}

// Tracks the last known model count so checkLocalInferenceStatus doesn't need to re-fetch
let _lastModelCount = undefined;

function updateLMStatusBar(connected, modelCount) {
    // Update the status bar on the dashboard if it exists
    let bar = document.getElementById('lmStatusBar');
    if (!bar) return;
    if (connected) {
        bar.className = 'lm-status-bar connected';
        bar.innerHTML = `<i class="fas fa-circle"></i> <strong>Local inference ready</strong> — ${escapeHtml(String(modelCount))} model${modelCount !== 1 ? 's' : ''} available`;
    } else {
        bar.className = 'lm-status-bar disconnected';
        bar.innerHTML = `<i class="fas fa-exclamation-circle"></i> <strong>Local inference unavailable</strong> — No <span class="vt-tooltip" data-tooltip="GGUF (GPT-Generated Unified Format) — a compact model file format optimized for fast local inference on consumer hardware">GGUF</span> models found. <a href="#" data-action="switchView" data-id="settings">Configure →</a>`;
    }
}

// ==================== VIEW STATE HELPERS ====================

/**
 * Show skeleton loading placeholders in a container.
 *
 * @param {string} containerId - The ID of the container element.
 * @param {string} template - Which skeleton template: 'cards', 'list', 'stats'.
 * @param {number} [count=3] - Number of skeleton items to show.
 */
function showSkeleton(containerId, template, count) {
    count = count || 3;
    var container = document.getElementById(containerId);
    if (!container) return;

    var html = '<div class="skeleton-group" data-skeleton="true">';
    for (var i = 0; i < count; i++) {
        if (template === 'cards') {
            html += '<div class="skeleton-card">'
                + '<div class="skeleton skeleton-line" style="width:60%;height:20px;"></div>'
                + '<div class="skeleton skeleton-line" style="width:100%;height:14px;"></div>'
                + '<div class="skeleton skeleton-line" style="width:80%;height:14px;"></div>'
                + '<div class="skeleton skeleton-line" style="width:40%;height:14px;"></div>'
                + '</div>';
        } else if (template === 'stats') {
            html += '<div class="skeleton-card skeleton-card--stat">'
                + '<div class="skeleton skeleton-line" style="width:50%;height:12px;"></div>'
                + '<div class="skeleton skeleton-line" style="width:30%;height:32px;margin:8px 0;"></div>'
                + '<div class="skeleton skeleton-line" style="width:60%;height:12px;"></div>'
                + '</div>';
        } else {
            // 'list' — default
            html += '<div class="skeleton-row">'
                + '<div class="skeleton skeleton-avatar"></div>'
                + '<div style="flex:1">'
                + '<div class="skeleton skeleton-line" style="width:70%;"></div>'
                + '<div class="skeleton skeleton-line" style="width:40%;"></div>'
                + '</div>'
                + '</div>';
        }
    }
    html += '</div>';
    container.innerHTML = html;
}

/**
 * Remove skeleton placeholders from a container.
 *
 * @param {string} containerId - The ID of the container element.
 */
function hideSkeleton(containerId) {
    var container = document.getElementById(containerId);
    if (!container) return;
    var skeleton = container.querySelector('[data-skeleton="true"]');
    if (skeleton) skeleton.remove();
}

/**
 * Show an error state with icon, message, and retry button inside a container.
 *
 * @param {string} containerId - The ID of the container element.
 * @param {string} message - Error message to display.
 * @param {function} retryFn - Function to call when Retry is clicked.
 */
function showErrorState(containerId, message, retryFn) {
    var container = document.getElementById(containerId);
    if (!container) return;

    container.innerHTML = '<div class="error-state">'
        + '<i class="fas fa-exclamation-triangle error-state__icon"></i>'
        + '<p class="error-state__message">' + escapeHtml(message) + '</p>'
        + '<button class="btn btn-primary error-state__retry">Retry</button>'
        + '</div>';

    var retryBtn = container.querySelector('.error-state__retry');
    if (retryBtn && retryFn) {
        retryBtn.addEventListener('click', retryFn);
    }
}

function switchView(view) {
    // Clear any lingering loading activities from previous views
    if (window.ActivityTracker) {
        ['load-dashboard', 'load-memory', 'load-training', 'load-models'].forEach(function(id) {
            ActivityTracker.completeActivity(id, 'Navigation');
        });
    }

    // Clear status banner when switching views (prevents stale error banners)
    hideStatusBanner();

    // Dispatch event for adaptive hints tracking
    document.dispatchEvent(new CustomEvent('vetinari:viewSwitch', { detail: { view: view } }));

    // Update nav
    document.querySelectorAll('.nav-item').forEach(item => {
        item.classList.remove('active');
        if (item.dataset.view === view) {
            item.classList.add('active');
        }
    });

    // Auto-expand Advanced nav if switching to an advanced view
    const advancedViews = ['workflow', 'agents', 'output', 'archive', 'tasks', 'decomposition'];
    if (advancedViews.includes(view)) {
        const toggle = document.getElementById('advancedNavToggle');
        const items = document.getElementById('advancedNavItems');
        if (toggle && items && toggle.getAttribute('aria-expanded') === 'false') {
            toggle.setAttribute('aria-expanded', 'true');
            items.style.display = 'flex';
            items.style.flexDirection = 'column';
            items.style.gap = '4px';
            const chevron = document.getElementById('advancedNavChevron');
            if (chevron) chevron.style.transform = 'rotate(90deg)';
        }
    }

    // Update view
    document.querySelectorAll('.view').forEach(v => {
        v.classList.remove('active');
    });
    document.getElementById(view + 'View').classList.add('active');

    // Update URL to reflect current view (enables browser back/forward)
    const viewPath = view === 'prompt' ? '/' : `/${view}`;
    if (window.location.pathname !== viewPath) {
        history.pushState({ view: view }, '', viewPath);
    }

    // Update header title
    const titles = {
        prompt: 'Chat',
        models: 'Models',
        training: 'Training',
        memory: 'Memory',
        settings: 'Settings',
        dashboard: 'Dashboard',
        workflow: 'Projects',
        agents: 'Agents',
        output: 'Output',
        archive: 'Archive',
        tasks: 'Tasks',
        decomposition: 'Plan Builder'
    };
    const titleEl = document.querySelector('.header-title h1') || document.querySelector('header h1') || document.querySelector('h1');
    if (titleEl) titleEl.textContent = titles[view] || view;

    // Dispatch view switch event for adaptive hints
    document.dispatchEvent(new CustomEvent('vetinari:viewSwitch', { detail: { view: view } }));

    // Show loading indicator
    const mainContent = document.querySelector('.main-content');
    mainContent.classList.add('loading');

    // Load view data
    switch(view) {
        case 'dashboard':
            loadDashboard();
            refreshAgentStatus();
            refreshSharedMemory();
            break;
        case 'models':
            loadModels();
            wireModelSearch();
            break;
        case 'tasks':
            loadTasks();
            break;
        case 'agents':
            loadAgentsView();
            break;
        case 'output':
            loadOutput();
            break;
        case 'archive':
            loadArchive();
            break;
        case 'training':
            loadTraining();
            break;
        case 'memory':
            loadMemory();
            break;
        case 'settings':
            loadSettings();
            loadModelConfig();
            loadCredentials();
            if (typeof loadSettingsRules === 'function') loadSettingsRules();
            if (typeof loadSdSettings === 'function') loadSdSettings();
            break;
        case 'prompt':
            loadPrompt();
            if (typeof detectHardware === 'function' && !currentProjectId) detectHardware();
            break;
        case 'workflow':
            loadWorkflow();
            break;
        case 'decomposition':
            loadDecomposition();
            break;
    }

    // Hide loading indicator after a short delay
    setTimeout(() => {
        mainContent.classList.remove('loading');
    }, 300);

    currentView = view;
}

// Prompt View - Chat Interface
let currentProjectId = null;
let systemPrompts = [];

async function loadPrompt() {
    const modelSelect = document.getElementById('chatModelSelect');
    const systemPromptSelect = document.getElementById('systemPromptSelect');

    try {
        // Load models, system prompts, and projects in parallel
        const [modelsRes, promptsRes, projectsRes] = await Promise.all([
            fetch('/api/v1/models'),
            fetch('/api/v1/system-prompts'),
            fetch('/api/projects')
        ]);

        const modelsData = await safeJsonParse(modelsRes);
        const promptsData = await safeJsonParse(promptsRes);
        const projectsData = await safeJsonParse(projectsRes);

        // Check for errors
        if (modelsData.error) {
            console.error('Error loading models:', modelsData.error);
            addActivity('Error loading models', 'error');
        }
        if (promptsData.error) {
            console.error('Error loading prompts:', promptsData.error);
        }
        if (projectsData.error) {
            console.error('Error loading projects:', projectsData.error);
        }

        models = modelsData.models || [];
        systemPrompts = promptsData.prompts || [];

        // Populate model select — use id as value, name as display
        const modelOptions = models.length > 0
            ? models.map(m => `<option value="${m.id || m.name}">${m.name || m.id}</option>`).join('')
            : '<option value="">No models found — click Discover</option>';
        if (modelSelect) modelSelect.innerHTML = modelOptions;

        // Populate system prompts select
        let promptOptions = '<option value="">Default System Prompt</option>';
        promptOptions += systemPrompts.map(p =>
            `<option value="${p.content}">${p.name}</option>`
        ).join('');
        if (systemPromptSelect) systemPromptSelect.innerHTML = promptOptions;

        // Load projects list
        loadProjectsList(projectsData.projects || []);

        // If there's a current project selected, load its details
        if (currentProjectId) {
            const project = (projectsData.projects || []).find(p => p.id === currentProjectId);
            if (project) {
                // Set the model select
                if (modelSelect && project.model) {
                    modelSelect.value = project.model;
                }
                if (project.active_model_id && modelSelect) {
                    modelSelect.value = project.active_model_id;
                }
                // Show tasks if available
                if (project.tasks && project.tasks.length > 0) {
                    renderTasks(project.tasks);
                }
                // Load full project details including conversation
                loadProjectDetails(currentProjectId);
            }
        }

    } catch (error) {
        console.error('Error loading prompt view:', error);
        addActivity('Error loading prompt view', 'error');
    }
}

async function loadProjectDetails(projectId) {
    try {
        const res = await fetch(`/api/project/${projectId}`);
        const data = await safeJsonParse(res);

        if (data.error) {
            console.error('Error loading project:', data.error);
            return;
        }

        if (data.conversation) {
            renderConversation(data.conversation);
        }

        if (data.tasks && data.tasks.length > 0) {
            window.currentProjectTasks = data.tasks;
            renderTasks(data.tasks);
        }

    } catch (error) {
        console.error('Error loading project details:', error);
    }
}

function loadProjectsList(projects) {
    const list = document.getElementById('projectsList');
    if (!list) return;

    if (projects.length === 0) {
        list.innerHTML = '<p style="padding: 1rem; color: var(--secondary);">No projects yet. Click "New" to start!</p>';
        return;
    }

    // Retrieve pinned project IDs from localStorage
    let pinnedIds = [];
    try { pinnedIds = JSON.parse(localStorage.getItem('vetinari_pinned_projects') || '[]'); } catch(e) { /* ignore */ }

    // Partition into pinned and unpinned
    const pinned = projects.filter(p => pinnedIds.includes(p.id));
    const unpinned = projects.filter(p => !pinnedIds.includes(p.id));

    // Group unpinned projects by date
    const now = new Date();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const yesterday = new Date(today.getTime() - 86400000);
    const weekAgo = new Date(today.getTime() - 7 * 86400000);

    const groups = { 'Today': [], 'Yesterday': [], 'This Week': [], 'Older': [] };
    unpinned.forEach(p => {
        const d = new Date(p.updated_at || p.created_at || 0);
        if (d >= today) groups['Today'].push(p);
        else if (d >= yesterday) groups['Yesterday'].push(p);
        else if (d >= weekAgo) groups['This Week'].push(p);
        else groups['Older'].push(p);
    });

    // Render helper for a single project item
    const renderItem = (p) => `
        <div class="project-item ${p.id === currentProjectId ? 'active' : ''}${pinnedIds.includes(p.id) ? ' pinned' : ''}" data-id="${p.id}">
            <div class="project-name">${pinnedIds.includes(p.id) ? '<i class="fas fa-thumbtack" style="margin-right:4px;font-size:0.65rem;color:var(--warning)"></i>' : ''}${escapeHtml(p.name)}</div>
            <div class="project-meta">${p.message_count || 0} messages</div>
        </div>`;

    let html = '';

    // Pinned section
    if (pinned.length > 0) {
        html += '<div class="sidebar-group-header">Pinned</div>';
        html += pinned.map(renderItem).join('');
    }

    // Date-grouped sections
    for (const [label, items] of Object.entries(groups)) {
        if (items.length > 0) {
            html += `<div class="sidebar-group-header">${label}</div>`;
            html += items.map(renderItem).join('');
        }
    }

    list.innerHTML = html;

    // Add click handlers
    list.querySelectorAll('.project-item').forEach(item => {
        item.addEventListener('click', () => {
            const projectId = item.dataset.id;
            selectProject(projectId);
        });
    });
}

// Load projects in sidebar (persistent)
async function loadSidebarProjects() {
    const list = document.getElementById('sidebarProjectsList');
    if (!list) return;

    try {
        const res = await fetch('/api/projects');
        const data = await safeJsonParse(res);

        if (data.error || !data.projects) {
            list.innerHTML = '<p style="padding: 0.5rem; font-size: 0.75rem; color: var(--text-secondary);">No projects</p>';
            return;
        }

        const projects = data.projects;

        if (projects.length === 0) {
            list.innerHTML = '<p style="padding: 0.5rem; font-size: 0.75rem; color: var(--text-secondary);">No projects yet</p>';
            return;
        }

        list.innerHTML = projects.map(p => {
            const status = p.status || 'pending';
            const taskCount = p.tasks?.length || 0;
            const completedCount = p.tasks?.filter(t => t.status === 'completed').length || 0;

            return `
                <div class="sidebar-project-item ${p.id === currentProjectId ? 'active' : ''}" data-id="${escapeHtml(p.id)}" data-action="selectProjectFromSidebar">
                    <div class="sidebar-project-info">
                        <span class="sidebar-project-name">${escapeHtml(p.name)}</span>
                        <div class="sidebar-project-status">
                            <span class="status-dot status-${status}"></span>
                            <span>${completedCount}/${taskCount} tasks</span>
                        </div>
                    </div>
                    <div class="sidebar-project-actions">
                        <button class="btn btn-icon btn-secondary" data-action="quickRename" data-id="${escapeHtml(p.id)}" data-stop-propagation="true" title="Rename">
                            <i class="fas fa-edit"></i>
                        </button>
                        <button class="btn btn-icon btn-secondary" data-action="archiveProject" data-id="${escapeHtml(p.id)}" data-stop-propagation="true" title="Archive">
                            <i class="fas fa-archive"></i>
                        </button>
                    </div>
                </div>
            `;
        }).join('');

    } catch (error) {
        if (window._serverConnected) console.error('Error loading sidebar projects:', error);
    }
}

async function quickRename(projectId) {
    const newName = prompt('Enter new project name:');
    if (!newName) return;

    try {
        const res = await fetch(`/api/project/${projectId}/rename`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
            body: JSON.stringify({ name: newName })
        });
        const data = await safeJsonParse(res);

        if (data.status === 'renamed') {
            addActivity(`Project renamed to: ${newName}`);
            loadSidebarProjects();
        }
    } catch (error) {
        console.error('Error renaming project:', error);
    }
}

window.selectProjectFromSidebar = function(projectId) {
    selectProject(projectId);
    switchView('prompt');
};

async function selectProject(projectId) {
    currentProjectId = projectId;

    // Update sidebar selection
    document.querySelectorAll('.project-item, .sidebar-project-item').forEach(item => {
        item.classList.toggle('active', item.dataset.id === projectId);
    });

    // Hide intake flow — a project is now active
    if (window.IntakeFlow) window.IntakeFlow.hide();

    try {
        const res = await fetch(`/api/project/${projectId}`);
        const data = await safeJsonParse(res);

        if (data.error) {
            console.error('Error loading project:', data.error);
            return;
        }

        // Update header to show project name
        const headerEl = document.getElementById('chatProjectTitle');
        // Show a clean project title: first line only, strip "Project:" prefix
        var rawTitle = data.name || data.goal || projectId;
        var cleanTitle = rawTitle.split('\n')[0].replace(/^Project:\s*/i, '').trim();
        if (headerEl) headerEl.textContent = cleanTitle || projectId;

        // Update chat input placeholder
        const chatInput = document.getElementById('chatInput');
        if (chatInput) chatInput.placeholder = 'Ask a follow-up...';

        if (data.conversation) {
            renderConversation(data.conversation);
        }

        if (data.tasks && data.tasks.length > 0) {
            renderTasks(data.tasks);
        }

        // Set selected model
        if (data.config && data.config.model) {
            const modelSelect = document.getElementById('chatModelSelect');
            if (modelSelect) modelSelect.value = data.config.model;
        }

    } catch (error) {
        console.error('Error loading project:', error);
    }
}

function renderConversation(conversation, append = false) {
    const messagesEl = document.getElementById('chatMessages');
    if (!messagesEl) return;

    if (!conversation || conversation.length === 0) {
        if (!append) {
            messagesEl.innerHTML = `
                <div class="chat-empty">
                    <i class="fas fa-comments"></i>
                    <p>Start a conversation</p>
                </div>
            `;
        }
        return;
    }

    // Delegate to appendChatMessage for consistent agent attribution + actions
    const useAppend = typeof window.appendChatMessage === 'function';

    if (append) {
        const existingMessages = messagesEl.querySelectorAll('.chat-message');
        const existingCount = existingMessages.length;
        const newMessages = conversation.slice(existingCount);

        newMessages.forEach(msg => {
            if (useAppend) {
                window.appendChatMessage(msg.role, msg.content, {
                    agent: msg.agent || msg.agent_type || null,
                    timestamp: msg.timestamp || null,
                    reasoning: msg.reasoning || null
                });
            } else {
                const msgEl = document.createElement('div');
                msgEl.className = `chat-message ${msg.role}`;
                msgEl.setAttribute('lang', 'en');
                msgEl.innerHTML = `
                    <div class="chat-message-avatar">
                        <i class="fas fa-${msg.role === 'user' ? 'user' : 'robot'}"></i>
                    </div>
                    <div class="chat-message-content">
                        ${formatMessageContent(msg.content)}
                    </div>
                `;
                messagesEl.appendChild(msgEl);
            }
        });
    } else {
        // Full render - clear then append each message
        messagesEl.innerHTML = '';
        conversation.forEach(msg => {
            if (useAppend) {
                window.appendChatMessage(msg.role, msg.content, {
                    agent: msg.agent || msg.agent_type || null,
                    timestamp: msg.timestamp || null,
                    reasoning: msg.reasoning || null
                });
            } else {
                const msgEl = document.createElement('div');
                msgEl.className = `chat-message ${msg.role}`;
                msgEl.setAttribute('lang', 'en');
                    <div class="chat-message-avatar">
                        <i class="fas fa-${msg.role === 'user' ? 'user' : 'robot'}"></i>
                    </div>
                    <div class="chat-message-content">
                        ${formatMessageContent(msg.content)}
                    </div>
                `;
                messagesEl.appendChild(msgEl);
            }
        });
    }

    // Scroll to bottom
    messagesEl.scrollTop = messagesEl.scrollHeight;
}

/**
 * Extract code blocks from a response string and open CodeCanvas if multiple blocks found.
 * Single short blocks (<20 lines) are left inline. Multiple or long blocks open the canvas.
 *
 * @param {string} responseText - The raw response text to scan for code fences.
 */
function maybeOpenCodeCanvas(responseText) {
    if (!responseText || !window.CodeCanvas) return;

    var CODE_FENCE_RE = /```(\w+)?\n([\s\S]*?)```/g;
    var blocks = [];
    var match;

    while ((match = CODE_FENCE_RE.exec(responseText)) !== null) {
        // Extract filename from preceding line: look for `# filename.ext`,
        // `### filename.ext`, or `<!-- filename.ext -->` patterns before the fence
        var precedingText = responseText.substring(
            Math.max(0, match.index - 200), match.index
        );
        var nameFromHeading = _extractFilenameFromContext(precedingText);
        blocks.push({
            language: match[1] || 'text',
            content: match[2].trim(),
            contextFilename: nameFromHeading
        });
    }

    // Only open canvas for multiple blocks or a single long block (20+ lines)
    if (blocks.length === 0) return;
    if (blocks.length === 1 && blocks[0].content.split('\n').length < 20) return;

    // Build file list with best-effort filenames
    var EXT_MAP = { python: 'py', javascript: 'js', typescript: 'ts', html: 'html', css: 'css', java: 'java', rust: 'rs', go: 'go', bash: 'sh', shell: 'sh', sql: 'sql', yaml: 'yaml', json: 'json', ruby: 'rb', cpp: 'cpp', c: 'c' };
    var usedNames = {};
    var files = blocks.map(function(block, idx) {
        var ext = EXT_MAP[block.language] || block.language || 'txt';
        var filename = block.contextFilename || ('file-' + (idx + 1) + '.' + ext);
        // Deduplicate filenames by appending a counter if needed
        if (usedNames[filename]) {
            usedNames[filename]++;
            var dotIdx = filename.lastIndexOf('.');
            if (dotIdx > 0) {
                filename = filename.substring(0, dotIdx) + '-' + usedNames[filename] + filename.substring(dotIdx);
            } else {
                filename = filename + '-' + usedNames[filename];
            }
        } else {
            usedNames[filename] = 1;
        }
        return {
            filename: filename,
            language: block.language,
            content: block.content
        };
    });

    CodeCanvas.open(files);
}

/**
 * Extract a filename from text preceding a code fence.
 *
 * Scans the last line(s) before the fence for patterns like:
 *   # filename.ext           (markdown heading)
 *   ### path/to/filename.ext (deeper heading)
 *   <!-- filename.ext -->    (HTML comment)
 *   // filename.ext          (code comment)
 *   `filename.ext`           (inline code)
 *
 * @param {string} text - The text preceding the code fence (up to ~200 chars).
 * @returns {string|null} The extracted filename, or null if none found.
 */
function _extractFilenameFromContext(text) {
    if (!text) return null;
    // Split into lines, take the last few non-empty lines before the fence
    var lines = text.split('\n').filter(function(l) { return l.trim().length > 0; });
    if (lines.length === 0) return null;

    // Check the last 3 lines for filename patterns (closest to fence first)
    var checkLines = lines.slice(-3).reverse();
    // Matches: heading with filename, inline code filename, comment filename
    var FILENAME_RE = /(?:^#{1,6}\s+|`|\/\/\s*|<!--\s*)([a-zA-Z0-9_\-./]+\.[a-zA-Z0-9]+)/;
    for (var i = 0; i < checkLines.length; i++) {
        var m = checkLines[i].match(FILENAME_RE);
        if (m && m[1]) {
            // Strip path separators to get just the filename
            var parts = m[1].split('/');
            return parts[parts.length - 1];
        }
    }
    return null;
}

function formatMessageContent(content) {
    if (!content) return '';

    // Detect raw plan dict/JSON responses and format them as a plan summary
    var trimmed = content.trimStart();
    if (trimmed.startsWith("{'plan_id'") || trimmed.startsWith('{"plan_id"')) {
        try {
            // Convert Python dict syntax to JSON
            var jsonStr = content.replace(/'/g, '"').replace(/True/g, 'true').replace(/False/g, 'false').replace(/None/g, 'null');
            var plan = JSON.parse(jsonStr);
            var planId = escapeHtml(plan.plan_id || plan.project_id || '');
            var html = '<div class="plan-summary" data-plan-id="' + planId + '">';
            html += '<strong>Plan Created</strong>';
            if (plan.goal) html += '<p>' + escapeHtml(plan.goal) + '</p>';
            if (plan.tasks && plan.tasks.length > 0) {
                html += '<div class="plan-tasks"><strong>' + plan.tasks.length + ' Tasks:</strong><ol>';
                plan.tasks.forEach(function(t, idx) {
                    var deps = (t.depends_on && t.depends_on.length) ? ' <span class="task-dep">(depends on: ' + escapeHtml(t.depends_on.join(', ')) + ')</span>' : '';
                    var status = t.status ? ' <span class="task-status task-status-' + escapeHtml(t.status) + '">' + escapeHtml(t.status) + '</span>' : '';
                    html += '<li>' + escapeHtml(t.description || t.id || 'Task') + deps + status + '</li>';
                });
                html += '</ol></div>';
            }
            // Plan status display
            var planStatus = plan.status || 'draft';
            html += '<div class="plan-status-bar">';
            html += '<span class="plan-status plan-status-' + escapeHtml(planStatus) + '">' + escapeHtml(planStatus.toUpperCase()) + '</span>';
            html += '</div>';
            // Approve & Execute / Reject buttons for non-running plans
            if (planId && planStatus !== 'running' && planStatus !== 'completed') {
                html += '<div class="plan-actions">';
                html += '<button class="btn btn-small btn-success plan-approve-btn" data-plan-id="' + planId + '" onclick="approvePlan(\'' + planId + '\')">Approve &amp; Execute</button>';
                html += '<button class="btn btn-small btn-danger plan-reject-btn" data-plan-id="' + planId + '" onclick="rejectPlan(\'' + planId + '\')">Reject</button>';
                html += '</div>';
            }
            html += '</div>';
            return html;
        } catch (e) {
            // Fall through to normal formatting if parse fails
        }
    }

    // Extract code blocks first (before HTML escaping) so we can escape
    // code content separately and protect the rest from injection.
    const codeBlocks = [];
    let processed = content.replace(/```(\w+)?\n([\s\S]*?)```/g, (match, lang, code) => {
        const idx = codeBlocks.length;
        codeBlocks.push(`<pre><code class="language-${escapeHtml(lang || 'text')}">${escapeHtml(code.trim())}</code></pre>`);
        return `\x00CODE${idx}\x00`;
    });

    // Escape the non-code portion to prevent XSS
    processed = escapeHtml(processed);

    // Restore code blocks (already escaped)
    processed = processed.replace(/\x00CODE(\d+)\x00/g, (_, idx) => codeBlocks[parseInt(idx, 10)]);

    // Convert inline code (content already escaped above)
    processed = processed.replace(/`([^`]+)`/g, '<code>$1</code>');

    // Convert newlines to <br>
    processed = processed.replace(/\n/g, '<br>');

    return processed;
}

const _escapeDiv = document.createElement('div');
function escapeHtml(text) {
    _escapeDiv.textContent = text;
    return _escapeDiv.innerHTML;
}

let currentDepthLimit = 12;
let expandedTasks = new Set();

function renderTasks(tasks) {
    const tasksEl = document.getElementById('chatTasks');
    const tasksList = document.getElementById('tasksList');
    if (!tasksEl || !tasksList) return;

    tasksEl.style.display = 'block';

    const badge = document.getElementById('tasksCountBadge');
    if (badge) badge.textContent = (tasks && tasks.length) ? tasks.length : 0;

    if (!tasks || tasks.length === 0) {
        tasksList.innerHTML = '<p class="no-tasks">No tasks yet</p>';
        return;
    }

    const depthLimit = currentDepthLimit;

    tasksList.innerHTML = tasks.map(task => {
        const depth = task.depth || 0;
        const isHidden = depth > depthLimit;
        const hasChildren = task.children && task.children.length > 0;
        const isExpanded = expandedTasks.has(task.id);
        const showChildren = isExpanded && hasChildren;

        return `
            <div class="task-item ${task.status || 'pending'} depth-${Math.min(depth, 12)} ${isHidden ? 'hidden-depth' : ''}" lang="en" data-task-id="${task.id}" data-depth="${depth}" data-action="showModelRanking" data-id="${task.id}" data-arg2="${encodeURIComponent(task.description || '')}">
                ${hasChildren ? `<span class="task-expand" role="button" tabindex="0" aria-label="${isExpanded ? 'Collapse' : 'Expand'} task ${escapeHtml(task.id)}" aria-expanded="${isExpanded ? 'true' : 'false'}" data-action="toggleTaskExpand" data-id="${task.id}" data-stop-propagation="true"><i class="fas ${isExpanded ? 'fa-chevron-down' : 'fa-chevron-right'}"></i></span>` : '<span style="width: 1rem; display: inline-block;"></span>'}
                <span class="task-status ${task.output || task.status === 'completed' ? 'completed' : 'pending'}"></span>
                <div class="task-info">
                    <span class="task-name">${escapeHtml(task.id)}</span>
                    ${task.description ? `<span class="task-desc">${escapeHtml(task.description.substring(0, 60))}...</span>` : ''}
                    ${task.assigned_model ? `<span class="task-model"><i class="fas fa-microchip"></i> ${escapeHtml(task.assigned_model)}</span>` : ''}
                    ${task.model_override ? `<span class="task-override"><i class="fas fa-user"></i> override</span>` : ''}
                </div>
                ${task.files && task.files.length > 0 ? `<span class="task-files">(${task.files.length} files)</span>` : ''}
            </div>
        `;
    }).join('');
}

function toggleTaskExpand(taskId) {
    if (expandedTasks.has(taskId)) {
        expandedTasks.delete(taskId);
    } else {
        expandedTasks.add(taskId);
    }
    loadCurrentProject();
}

function updateDepthView(depth) {
    currentDepthLimit = parseInt(depth);
    const depthEl = document.getElementById('depthValue');
    if (depthEl) depthEl.textContent = depth;
    renderTasks(window.currentProjectTasks || []);
}

function expandAllTasks() {
    const tasks = window.currentProjectTasks || [];
    tasks.forEach(task => {
        if (task.children && task.children.length > 0) {
            expandedTasks.add(task.id);
        }
    });
    renderTasks(tasks);
}

function collapseAllTasks() {
    expandedTasks.clear();
    renderTasks(window.currentProjectTasks || []);
}

let currentSelectedTaskId = null;

function showModelRanking(taskId, taskDescription) {
    const panel = document.getElementById('modelRankingPanel');
    if (!panel) return;

    currentSelectedTaskId = taskId;
    panel.style.display = 'block';

    // Move into context panel and auto-open
    if (typeof window.moveToContextPanel === 'function') {
        window.moveToContextPanel(panel);
    }

    const listEl = document.getElementById('modelRankingList');
    listEl.innerHTML = '<div class="loading"><div class="spinner"></div> Loading models...</div>';

    const overrideSection = document.getElementById('modelOverrideSection');
    overrideSection.style.display = 'block';

    const description = decodeURIComponent(taskDescription || '');

    fetch(`/api/v1/project/${currentProjectId}/model-search`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest'},
        body: JSON.stringify({task_description: description})
    })
    .then(res => res.json())
    .then(data => {
        if (data.error) {
            listEl.innerHTML = '<p style="color: var(--error);">Error: ' + escapeHtml(data.error) + '</p>';
            return;
        }

        const candidates = data.candidates || [];

        listEl.innerHTML = candidates.map((c, i) => `
            <div class="model-candidate-item" data-action="selectOverrideModel" data-id="${escapeHtml(c.id)}" data-arg2="${escapeHtml(c.name)}" data-hover-action="rationale">
                <span class="model-candidate-rank">#${i + 1}</span>
                <div class="model-candidate-info">
                    <div class="model-candidate-name">${escapeHtml(c.name || c.id)}</div>
                    <div class="model-candidate-source">${escapeHtml(c.source_type || '')}</div>
                </div>
                <div class="model-candidate-scores">
                    <span class="model-candidate-score hard-data" title="Hard Data">HD: ${VFmt.decimal(c.hard_data_score || 0, 2)}</span>
                    <span class="model-candidate-score benchmark" title="Benchmarks">BM: ${VFmt.decimal(c.benchmark_score || 0, 2)}</span>
                    <span class="model-candidate-score sentiment" title="Sentiment">SN: ${VFmt.decimal(c.sentiment_score || 0, 2)}</span>
                </div>
                <span class="model-candidate-final">${VFmt.decimal(c.final_score || 0, 2)}</span>
                <div class="model-candidate-rationale">
                    <strong>Why:</strong> ${escapeHtml(c.short_rationale || 'No rationale available')}
                    ${c.provenance && c.provenance.length > 0 ? `<br><a href="${escapeHtml(c.provenance[0].url)}" target="_blank" rel="noopener noreferrer" style="color: var(--primary-light);">View Source</a>` : ''}
                </div>
            </div>
        `).join('');

        const select = document.getElementById('overrideModelSelect');
        select.innerHTML = '<option value="">Select model...</option>' +
            candidates.map(c => `<option value="${escapeHtml(c.id)}">${escapeHtml(c.name || c.id)} (${VFmt.decimal(c.final_score || 0, 2)})</option>`).join('');
    })
    .catch(err => {
        listEl.innerHTML = '<p style="color: var(--error);">Error loading models</p>';
    });
}

function showRationale(element) {
    element.classList.add('show-rationale');
}

function hideRationale(element) {
    element.classList.remove('show-rationale');
}

function refreshModelSearch() {
    if (currentSelectedTaskId && window.currentProjectTasks) {
        const task = window.currentProjectTasks.find(t => t.id === currentSelectedTaskId);
        if (task) {
            showModelRanking(currentSelectedTaskId, task.description);
        }
    }
}

function selectOverrideModel(modelId, modelName) {
    const select = document.getElementById('overrideModelSelect');
    select.value = modelId;
}

function applyModelOverride() {
    const select = document.getElementById('overrideModelSelect');
    const modelId = select.value;

    if (!modelId || !currentSelectedTaskId) {
        alert('Please select a task and model');
        return;
    }

    fetch(`/api/v1/project/${currentProjectId}/task/${currentSelectedTaskId}/override`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest'},
        body: JSON.stringify({model_id: modelId})
    })
    .then(res => res.json())
    .then(data => {
        if (data.error) {
            alert('Error: ' + data.error);
            return;
        }

        showStatusBanner('Model override applied successfully', 'success');

        loadProjectDetails(currentProjectId);
    })
    .catch(err => {
        alert('Error applying override');
    });
}

// Progress rendering functions
function showProgress(tasks) {
    const progressSection = document.getElementById('progressSection');
    if (!progressSection) return;

    progressSection.style.display = 'block';

    // Calculate overall progress
    const total = tasks.length;
    const completed = tasks.filter(t => t.status === 'completed').length;
    const percent = total > 0 ? Math.round((completed / total) * 100) : 0;

    // Update overall progress bar
    const progressBar = document.getElementById('overallProgressBar');
    const progressPercent = document.getElementById('progressPercent');
    if (progressBar) progressBar.style.width = percent + '%';
    if (progressPercent) progressPercent.textContent = percent + '%';

    // Render per-task progress
    renderTaskProgress(tasks);
}

function renderTaskProgress(tasks) {
    const tasksEl = document.getElementById('chatTasks');
    if (!tasksEl) return;

    // Find or create task progress container
    let progressContainer = document.getElementById('taskProgressContainer');
    if (!progressContainer) {
        const container = document.createElement('div');
        container.id = 'taskProgressContainer';
        container.style.marginTop = '1rem';
        tasksEl.appendChild(container);
        progressContainer = container;
    }

    progressContainer.innerHTML = tasks.map(task => {
        const statusClass = task.status === 'completed' ? 'completed' : (task.status === 'running' ? 'running' : 'pending');
        const progress = task.status === 'completed' ? 100 : (task.status === 'running' ? 50 : 0);

        let subtasksHtml = '';
        if (task.subtasks && task.subtasks.length > 0) {
            subtasksHtml = `<div class="subtasks" id="subtasks-${task.id}">` +
                task.subtasks.map(st => `
                    <div class="subtask-item">
                        <span class="task-status ${st.status === 'completed' ? 'completed' : (st.status === 'running' ? 'running' : 'pending')}"></span>
                        <span class="task-id">${st.id}</span>
                        <span>${st.description ? st.description.substring(0, 30) + '...' : ''}</span>
                        <span class="task-model">${st.assigned_model || ''}</span>
                    </div>
                `).join('') +
                '</div>';
        }

        return `
            <div class="task-progress-item">
                <div class="task-progress-header" role="button" tabindex="0" aria-expanded="false" aria-controls="subtasks-${task.id}" data-action="toggleSubtasks" data-id="${task.id}">
                    <div>
                        <span class="task-id">${task.id}</span>
                        <span class="task-model">${task.assigned_model || ''}</span>
                    </div>
                    <span>${task.description ? task.description.substring(0, 40) + '...' : ''}</span>
                </div>
                <div class="task-progress-bar-container">
                    <div class="task-progress-bar ${statusClass}" style="width: ${progress}%;"></div>
                </div>
                ${subtasksHtml}
            </div>
        `;
    }).join('');
}

function toggleSubtasks(taskId) {
    const subtasksEl = document.getElementById(`subtasks-${taskId}`);
    if (subtasksEl) {
        const expanded = subtasksEl.classList.toggle('expanded');
        const trigger = document.querySelector(`.task-progress-header[data-id="${taskId}"]`);
        if (trigger) trigger.setAttribute('aria-expanded', expanded ? 'true' : 'false');
    }
}

function updateWorkingOn(taskId, model, status) {
    const workingOn = document.getElementById('workingOnText');
    if (!workingOn) return;

    if (status === 'running') {
        workingOn.textContent = `Working on ${taskId} using ${model || 'auto-selected model'}...`;
    } else if (status === 'completed') {
        workingOn.textContent = `Completed ${taskId}`;
    } else {
        workingOn.textContent = 'Waiting...';
    }
}

// Track active fetch controller for stop functionality
let activeFetchController = null;

// Rough context window usage estimator (characters / 4 ≈ tokens)
let estimatedTokens = 0;
const MAX_CONTEXT_TOKENS = 8192; // conservative default

function updateContextIndicator() {
    const fill = document.getElementById('contextBarFill');
    const label = document.getElementById('contextLabel');
    if (!fill || !label) return;

    // Count characters in all chat messages
    const messages = document.querySelectorAll('#chatMessages .chat-message-content');
    let totalChars = 0;
    messages.forEach(el => { totalChars += (el.textContent || '').length; });
    estimatedTokens = Math.round(totalChars / 4);

    const pct = Math.min((estimatedTokens / MAX_CONTEXT_TOKENS) * 100, 100);
    fill.style.width = pct + '%';

    // Color coding
    fill.classList.remove('warn', 'critical');
    if (pct > 80) fill.classList.add('critical');
    else if (pct > 50) fill.classList.add('warn');

    const kTokens = VFmt.decimal(estimatedTokens / 1000, 1);
    const kMax = VFmt.decimal(MAX_CONTEXT_TOKENS / 1000, 0);
    label.textContent = `~${kTokens}K / ${kMax}K tokens`;
}

async function sendChatMessage() {
    const input = document.getElementById('chatInput');
    const sendBtn = document.getElementById('sendMessageBtn');

    // Micro-interaction: brief scale bounce on send
    if (sendBtn) sendBtn.classList.add('btn-send-pulse');
    setTimeout(function() { if (sendBtn) sendBtn.classList.remove('btn-send-pulse'); }, 150);

    let message = input.value.trim();

    if (!message) return;

    // Clear saved draft on successful send
    localStorage.removeItem('vetinari_chat_draft');

    // Expand preset template variables before sending
    const modelSelect = document.getElementById('chatModelSelect');
    const modelName = modelSelect?.selectedOptions?.[0]?.text || modelSelect?.value || 'auto';
    const chatTitle = document.getElementById('chatProjectTitle');
    const projectName = chatTitle?.textContent || 'Untitled';
    message = message
        .replace(/\{\{CURRENT_DATE\}\}/g, new Date().toLocaleDateString())
        .replace(/\{\{MODEL_NAME\}\}/g, modelName)
        .replace(/\{\{PROJECT_NAME\}\}/g, projectName);

    // Clear chat draft from localStorage
    try { localStorage.removeItem('vetinari_chat_draft'); } catch(e) { /* ignore */ }

    const model = modelSelect?.value;

    // If no project selected, create a new project (intake flow is hidden after project creation succeeds)
    if (!currentProjectId) {
        await createNewProject(message, model);
        return;
    }

    // Add user message via enhanced appendChatMessage
    if (typeof window.appendChatMessage === 'function') {
        window.appendChatMessage('user', message);
    } else {
        const messagesEl = document.getElementById('chatMessages');
        const userMsg = document.createElement('div');
        userMsg.className = 'chat-message user chat-message-enter';
        userMsg.innerHTML = `
            <div class="chat-message-avatar"><i class="fas fa-user"></i></div>
            <div class="chat-message-content">${escapeHtml(message)}</div>
        `;
        messagesEl.appendChild(userMsg);
    }

    // Switch send button to stop button
    if (sendBtn) {
        sendBtn.classList.add('btn-stop');
        sendBtn.classList.remove('btn-primary');
        sendBtn.innerHTML = '<i class="fas fa-stop"></i>';
        sendBtn.title = 'Stop generation';
    }

    // Show streaming cursor in a thinking placeholder
    const messagesEl = document.getElementById('chatMessages');
    const thinkingMsg = document.createElement('div');
    thinkingMsg.className = 'chat-message assistant';
    thinkingMsg.id = 'thinkingMsg';
    thinkingMsg.innerHTML = `
        <div class="chat-message-avatar"><i class="fas fa-robot"></i></div>
        <div class="chat-message-body">
            <div class="chat-message-content streaming-cursor"><i class="fas fa-spinner fa-spin"></i> Thinking</div>
        </div>
    `;
    messagesEl.appendChild(thinkingMsg);
    messagesEl.scrollTop = messagesEl.scrollHeight;

    // Disable input but keep stop button active
    input.disabled = true;

    // Create abort controller for stop functionality
    activeFetchController = new AbortController();

    // Stop button: swap send handler for stop handler
    function stopHandler() {
        if (activeFetchController) {
            activeFetchController.abort();
            activeFetchController = null;
        }
    }
    if (sendBtn) {
        sendBtn.removeEventListener('click', sendChatMessage);
        sendBtn.addEventListener('click', stopHandler);
    }

    try {
        const res = await fetch(`/api/project/${currentProjectId}/message`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
            body: JSON.stringify({ message }),
            signal: activeFetchController?.signal
        });
        const data = await safeJsonParse(res);

        // Remove thinking message
        document.getElementById('thinkingMsg')?.remove();
        activeFetchController = null;

        // Handle response using enhanced appendChatMessage
        if (!data) {
            addActivity('No response from server', 'error');
        } else if (data.status === 'ok' && data.response) {
            if (typeof window.appendChatMessage === 'function') {
                window.appendChatMessage('assistant', data.response, {
                    agent: data.agent || 'Vetinari',
                    reasoning: data.reasoning || null
                });
            } else {
                const aiMsg = document.createElement('div');
                aiMsg.className = 'chat-message assistant chat-message-enter';
                aiMsg.setAttribute('lang', 'en');
                aiMsg.innerHTML = `
                    <div class="chat-message-avatar"><i class="fas fa-robot"></i></div>
                    <div class="chat-message-content">${formatMessageContent(data.response)}</div>
                `;
                messagesEl.appendChild(aiMsg);
            }
            messagesEl.scrollTop = messagesEl.scrollHeight;
            // Auto-open CodeCanvas for multi-file or long code responses
            maybeOpenCodeCanvas(data.response);
        } else if (data.status === 'partial' || data.status === 'error') {
            const content = data.raw_output || data.error || 'Unknown error';
            if (typeof window.appendChatMessage === 'function') {
                window.appendChatMessage('assistant', content, { warning: true, agent: data.agent });
            }
            addActivity('Received partial response - review output above', 'warning');
        } else if (data.error) {
            addActivity(`Error: ${data.error}`, 'error');
        } else {
            addActivity('Unknown response format', 'error');
        }

    } catch (error) {
        document.getElementById('thinkingMsg')?.remove();
        activeFetchController = null;

        if (error.name === 'AbortError') {
            // User stopped generation
            if (typeof window.appendChatMessage === 'function') {
                window.appendChatMessage('assistant', 'Generation stopped by user.', { warning: true });
            }
        } else {
            console.error('Error sending message:', error);
            addActivity(`Error: ${error}`, 'error');
        }
    } finally {
        input.disabled = false;
        input.value = '';
        input.focus();
        // Restore send button: remove stop handler, re-add send handler
        if (sendBtn) {
            sendBtn.removeEventListener('click', stopHandler);
            sendBtn.classList.remove('btn-stop');
            sendBtn.classList.add('btn-primary');
            sendBtn.innerHTML = '<i class="fas fa-paper-plane"></i>';
            sendBtn.title = 'Send (Ctrl+Enter)';
            sendBtn.addEventListener('click', sendChatMessage);
        }

        // Update context window indicator
        updateContextIndicator();
    }
}

// Active AbortController for project creation requests
let _projectCreateController = null;

async function createNewProject(message, model, metadata) {
    // Permission check before project execution
    if (typeof checkPermission === 'function') {
        const allowed = await checkPermission('ProjectExecute');
        if (!allowed) return;
    }
    const sendBtn = document.getElementById('sendMessageBtn');
    const input = document.getElementById('chatInput');
    const systemPrompt = document.getElementById('systemPromptInput')?.value || '';

    addActivity('Analyzing goal and creating project plan...');

    // Show thinking in chat
    const messagesEl = document.getElementById('chatMessages');
    const thinkingMsg = document.createElement('div');
    thinkingMsg.className = 'chat-message assistant';
    thinkingMsg.id = 'thinkingMsg';
    thinkingMsg.innerHTML = `
        <div class="chat-message-avatar"><i class="fas fa-robot"></i></div>
        <div class="chat-message-content"><i class="fas fa-spinner fa-spin"></i> Analyzing goal and planning tasks...</div>
    `;
    messagesEl.innerHTML = '';
    messagesEl.appendChild(thinkingMsg);

    input.disabled = true;
    if (sendBtn) sendBtn.disabled = true;

    // Create AbortController for cancellation support
    if (_projectCreateController) _projectCreateController.abort();
    _projectCreateController = new AbortController();

    try {
        const res = await fetch('/api/new-project', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
            signal: _projectCreateController.signal,
            body: JSON.stringify(Object.assign(
                { goal: message, model, system_prompt: systemPrompt, auto_run: true },
                metadata || {}
            ))
        });
        const data = await safeJsonParse(res);

        document.getElementById('thinkingMsg')?.remove();

        if (!data) {
            messagesEl.innerHTML = `
                <div class="chat-empty">
                    <i class="fas fa-exclamation-circle"></i>
                    <p>No response from server</p>
                </div>
            `;
            addActivity('No response from server', 'error');
        } else if (data.error) {
            messagesEl.innerHTML = `
                <div class="chat-empty">
                    <i class="fas fa-exclamation-circle"></i>
                    <p>Error: ${escapeHtml(data.error)}</p>
                </div>
            `;
            addActivity('Error: ' + escapeHtml(data.error), 'error');
        } else if (data.needs_context) {
            // Show the follow-up question to the user
            const aiMsg = document.createElement('div');
            aiMsg.className = 'chat-message assistant';
            aiMsg.innerHTML = `
                <div class="chat-message-avatar"><i class="fas fa-robot"></i></div>
                <div class="chat-message-content">
                    <p>${data.follow_up_question || 'I need more context to create a proper plan. Could you provide more details?'}</p>
                    <div class="follow-up-prompt" style="margin-top: 1rem;">
                        <input type="text" class="input" id="followUpInput" placeholder="Provide more details..." style="width: 70%;">
                        <button class="btn btn-primary" data-action="submitFollowUp">Submit</button>
                    </div>
                </div>
            `;
            messagesEl.innerHTML = '';
            messagesEl.appendChild(aiMsg);
            messagesEl.scrollTop = messagesEl.scrollHeight;

            // Store the original message for follow-up
            window.pendingGoal = message;
            window.pendingModel = model;

            // Allow pressing Enter to submit
            document.getElementById('followUpInput')?.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') submitFollowUp();
            });

        } else if (data.project_id) {
            currentProjectId = data.project_id;

            // Hide intake flow now that a project is active
            if (window.IntakeFlow) {
                window.IntakeFlow.hide();
            }
            // Update header to show project name
            const titleEl = document.getElementById('chatProjectTitle');
            if (titleEl) titleEl.textContent = data.project_name || message.split('\n')[0] || 'Project';
            // Update chat input placeholder for follow-up mode
            const chatInputEl = document.getElementById('chatInput');
            if (chatInputEl) chatInputEl.placeholder = 'Ask a follow-up...';

            // Show the initial plan from the assistant's response
            if (data.conversation) {
                renderConversation(data.conversation);
            }

            // Show tasks in the task queue
            if (data.tasks && data.tasks.length > 0) {
                const tasks = data.tasks.map((t, i) => ({
                    id: t.id,
                    description: t.description,
                    assigned_model: t.assigned_model_id,
                    status: 'pending'
                }));
                renderTasks(tasks);
            }

            addActivity(`Project created: ${data.project_id} with ${data.tasks?.length || 0} tasks. Starting execution...`);

            // Show warnings if any
            if (data.warnings && data.warnings.length > 0) {
                showStatusBanner(data.warnings[0], 'warning');
            } else {
                showStatusBanner('Project created! Tasks are executing...', 'success');
            }

            // Poll for task completion status
            pollProjectStatus(data.project_id);

            // Refresh projects list
            loadSidebarProjects();

        } else {
            messagesEl.innerHTML = `
                <div class="chat-empty">
                    <i class="fas fa-exclamation-circle"></i>
                    <p>Unexpected response</p>
                </div>
            `;
        }

    } catch (error) {
        if (error.name === 'AbortError') {
            addActivity('Project creation cancelled', 'warning');
            messagesEl.innerHTML = `
                <div class="chat-empty">
                    <i class="fas fa-ban"></i>
                    <p>Project creation cancelled</p>
                </div>
            `;
        } else {
            console.error('Error creating project:', error);
            messagesEl.innerHTML = `
                <div class="chat-empty">
                    <i class="fas fa-exclamation-circle"></i>
                    <p>Error: ${escapeHtml(String(error))}</p>
                </div>
            `;
            addActivity('Error: ' + error, 'error');
        }
    } finally {
        _projectCreateController = null;
        input.disabled = false;
        if (sendBtn) sendBtn.disabled = false;
        input.value = '';
        input.focus();
    }
}

// Submit follow-up with more context
async function submitFollowUp() {
    const followUpInput = document.getElementById('followUpInput');
    if (!followUpInput) return;

    const additionalContext = followUpInput.value.trim();
    if (!additionalContext) {
        addActivity('Please provide more details', 'warning');
        return;
    }

    // Combine original goal with additional context
    const enhancedGoal = `${window.pendingGoal}\n\nAdditional context: ${additionalContext}`;

    // Clear the follow-up prompt and show thinking
    const messagesEl = document.getElementById('chatMessages');
    const thinkingMsg = document.createElement('div');
    thinkingMsg.className = 'chat-message assistant';
    thinkingMsg.id = 'thinkingMsg';
    thinkingMsg.innerHTML = `
        <div class="chat-message-avatar"><i class="fas fa-robot"></i></div>
        <div class="chat-message-content"><i class="fas fa-spinner fa-spin"></i> Creating plan with your additional context...</div>
    `;
    messagesEl.innerHTML = '';
    messagesEl.appendChild(thinkingMsg);

    // Disable input
    const input = document.getElementById('chatInput');
    const sendBtn = document.getElementById('sendMessageBtn');
    if (input) input.disabled = true;
    if (sendBtn) sendBtn.disabled = true;

    try {
        const res = await fetch('/api/new-project', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
            body: JSON.stringify({
                goal: enhancedGoal,
                model: window.pendingModel,
                system_prompt: document.getElementById('systemPromptInput')?.value || '',
                auto_run: true
            })
        });
        const data = await safeJsonParse(res);

        document.getElementById('thinkingMsg')?.remove();

        if (data.error) {
            messagesEl.innerHTML = `
                <div class="chat-empty">
                    <i class="fas fa-exclamation-circle"></i>
                    <p>Error: ${escapeHtml(data.error)}</p>
                </div>
            `;
            addActivity('Error: ' + escapeHtml(data.error), 'error');
        } else if (data.project_id) {
            currentProjectId = data.project_id;

            if (data.conversation) {
                renderConversation(data.conversation);
            }

            if (data.tasks && data.tasks.length > 0) {
                const tasks = data.tasks.map((t, i) => ({
                    id: t.id,
                    description: t.description,
                    assigned_model: t.assigned_model_id,
                    status: 'pending'
                }));
                renderTasks(tasks);
            }

            addActivity(`Project created with additional context: ${data.project_id}`);
            showStatusBanner('Project created! Tasks are executing...', 'success');
            pollProjectStatus(data.project_id);
            loadSidebarProjects();
        }

    } catch (error) {
        console.error('Error submitting follow-up:', error);
        addActivity('Error: ' + error, 'error');
    } finally {
        if (input) input.disabled = false;
        if (sendBtn) sendBtn.disabled = false;
        window.pendingGoal = null;
        window.pendingModel = null;
    }
}

window.submitFollowUp = submitFollowUp;
window.toggleSubtasks = toggleSubtasks;

// Poll for project status updates — skips when SSE is active for this project
function pollProjectStatus(projectId) {
    const pollInterval = setInterval(async () => {
        // Deduplicate: if SSE is connected for this project, skip HTTP polling
        if (window.SSEManager && SSEManager.isConnected && SSEManager.isConnected(`project-${projectId}`)) {
            return;
        }
        try {
            const res = await fetch(`/api/project/${projectId}`);
            const data = await safeJsonParse(res);

            if (data.error) {
                clearInterval(pollInterval);
                return;
            }

            // Update task status display with progress
            if (data.tasks && data.tasks.length > 0) {
                const tasksWithStatus = data.tasks.map(t => ({
                    id: t.id,
                    description: t.description,
                    output: t.output,
                    files: t.files,
                    assigned_model: t.assigned_model,
                    status: t.output ? 'completed' : (t.status === 'running' ? 'running' : 'pending'),
                    subtasks: t.subtasks || []
                }));

                renderTasks(tasksWithStatus);
                showProgress(tasksWithStatus);

                // Update "working on" indicator
                const runningTask = tasksWithStatus.find(t => t.status === 'running');
                if (runningTask) {
                    updateWorkingOn(runningTask.id, runningTask.assigned_model, 'running');
                } else {
                    const nextTask = tasksWithStatus.find(t => t.status === 'pending');
                    if (nextTask) {
                        updateWorkingOn(nextTask.id, nextTask.assigned_model, 'pending');
                    }
                }
            }

            // Update conversation - append any new messages
            if (data.conversation && data.conversation.length > 0) {
                renderConversation(data.conversation, true);
            }

            // Check if project is completed
            const config = data.config;
            if (config && config.status === 'completed') {
                clearInterval(pollInterval);
                showStatusBanner('All tasks completed! Final deliverable assembled.', 'success');
                addActivity('Project completed!');
                loadSidebarProjects();
                updateWorkingOn('', '', 'completed');
                // Hide progress spinner — work is done
                const ps = document.getElementById('progressSection');
                if (ps) ps.style.display = 'none';

                // Show final delivery panel
                if (config.final_delivery_path) {
                    showFinalDeliveryPanel(config, data.tasks);
                }
            } else if (config && config.status === 'error') {
                clearInterval(pollInterval);
                showStatusBanner('Project failed: ' + (config.error || 'Unknown error'), 'error');
                addActivity('Project failed: ' + (config.error || 'Unknown error'), 'error');
                // Hide progress on error too
                const ps = document.getElementById('progressSection');
                if (ps) ps.style.display = 'none';
            }

        } catch (error) {
            console.error('Error polling project status:', error);
        }
    }, 3000);  // Poll every 3 seconds

    // Stop polling after 5 minutes max
    setTimeout(() => clearInterval(pollInterval), 300000);
}

function showFinalDeliveryPanel(config, tasks) {
    const panel = document.getElementById('finalDeliveryPanel');
    const content = document.getElementById('finalDeliveryContent');

    if (!panel || !content) return;

    // Move into context panel and auto-open
    if (typeof window.moveToContextPanel === 'function') {
        window.moveToContextPanel(panel);
    }

    let taskHtml = '';
    if (tasks && tasks.length > 0) {
        tasks.forEach(t => {
            const status = t.output ? '✓' : '○';
            taskHtml += `
                <div class="final-delivery-task">
                    <h5>[${status}] ${escapeHtml(t.id)}: ${escapeHtml(t.description || 'No description')}</h5>
                    <span class="model-tag"><i class="fas fa-microchip"></i> ${escapeHtml(t.assigned_model || 'Not assigned')}</span>
                    ${t.files && t.files.length > 0 ? `<div style="margin-top: 0.5rem;"><strong>Generated Files:</strong> ${t.files.map(f => escapeHtml(f.name)).join(', ')}</div>` : ''}
                </div>
            `;
        });
    }

    content.innerHTML = `
        <div style="margin-bottom: 1rem;">
            <strong>Goal:</strong> ${escapeHtml(config.high_level_goal || 'N/A')}
        </div>
        <div style="margin-bottom: 1rem;">
            <strong>Final Report:</strong> <span class="report-unavailable">Report not available</span>
        </div>
        <div style="margin-bottom: 1rem;">
            <button class="btn btn-primary" data-action="downloadProject" data-id="${escapeHtml(config.project_name)}">
                <i class="fas fa-file-archive"></i> Download All
            </button>
        </div>
        <div>
            <strong>Tasks:</strong>
            ${taskHtml}
        </div>
    `;

    panel.style.display = 'block';
}

// Dashboard
async function loadDashboard() {
    showSkeleton('dashboardStatsGrid', 'stats');
    if (window.ActivityTracker) ActivityTracker.startActivity('load-dashboard', 'Loading dashboard');

    try {
        const [statusRes, modelsRes, projectsRes] = await Promise.all([
            fetch('/api/v1/status'),
            fetch('/api/v1/models'),
            fetch('/api/projects')
        ]);

        const status = await safeJsonParse(statusRes);
        const modelsData = await safeJsonParse(modelsRes);
        const projectsData = await safeJsonParse(projectsRes);

        const models = modelsData.models || [];
        const projects = projectsData.projects || [];
        _lastModelCount = models.length;   // Cache for local inference status check

        // Update stats
        document.getElementById('modelCount').textContent = models.length;

        // Calculate tasks from projects
        let totalTasks = 0;
        let completedTasks = 0;
        let runningTasks = 0;

        projects.forEach(p => {
            const tasks = p.tasks || [];
            totalTasks += tasks.length;
            tasks.forEach(t => {
                if (t.status === 'completed') completedTasks++;
                else if (t.status === 'running') runningTasks++;
            });
        });

        document.getElementById('taskCount').textContent = totalTasks;
        document.getElementById('completedCount').textContent = completedTasks;
        document.getElementById('runningCount').textContent = runningTasks;

        // Update local inference status bar
        updateLMStatusBar(models.length > 0, models.length);

        // Quick prompt removed (Phase 1) — Chat is the primary interaction now
        // Onboarding removed — Setup Wizard will handle first-boot (Phase 2)

        hideSkeleton('dashboardStatsGrid');
        if (window.ActivityTracker) ActivityTracker.completeActivity('load-dashboard', `${models.length} models, ${projects.length} projects`);
        addActivity(`Dashboard refreshed — ${models.length} models, ${projects.length} projects`);

        // Load hardware monitoring (non-blocking)
        loadGpuStats();
        loadSystemResources();

        // Populate dashboard cost tracking from token stats (non-blocking)
        fetch('/api/v1/token-stats').then(r => r.json()).then(ts => {
            var costEl = document.getElementById('costSession');
            if (costEl) costEl.textContent = VFmt.currency(ts.total_cost_usd || 0, 2);
            var costTodayEl = document.getElementById('costToday');
            if (costTodayEl) costTodayEl.textContent = VFmt.currency(ts.total_cost_usd || 0, 2);
        }).catch(() => {});

        // Populate dashboard training status (non-blocking)
        fetch('/api/v1/training/status').then(r => r.json()).then(tr => {
            var tStatusEl = document.getElementById('dashTrainingStatus');
            if (tStatusEl) tStatusEl.textContent = tr.is_training ? 'Training' : (tr.is_idle ? 'Idle' : 'Active');
            var tNextEl = document.getElementById('dashTrainingNextRun');
            if (tNextEl) tNextEl.textContent = tr.next_activity || 'Not scheduled';
        }).catch(() => {});

    } catch (error) {
        if (window._serverConnected) console.error('Error loading dashboard:', error);
        if (window.ActivityTracker) ActivityTracker.failActivity('load-dashboard', 'Failed to load dashboard');
        addActivity('Error loading dashboard', 'error');
        updateLMStatusBar(false, 0, '');
        showErrorState('dashboardStatsGrid', 'Failed to load dashboard data', loadDashboard);
    }
}

const _MAX_ACTIVITY_LOG = 50;
function addActivity(message, type = 'info') {
    const time = new Date().toLocaleTimeString();
    activityLog.unshift({ message, time, type });
    while (activityLog.length > _MAX_ACTIVITY_LOG) activityLog.pop();
    renderActivity();
}

function renderActivity() {
    const list = document.getElementById('activityList');
    if (!list) return;
    list.innerHTML = activityLog.map(item => `
        <div class="activity-item">
            <i class="fas fa-${item.type === 'error' ? 'exclamation-circle' : 'info-circle'}"></i>
            <span>${escapeHtml(item.message)}</span>
            <span class="time">${escapeHtml(item.time)}</span>
        </div>
    `).join('');
}

// Workflow
async function loadWorkflow() {
    const treeContainer = document.getElementById('workflowTree');
    treeContainer.innerHTML = '<div class="loading"><div class="spinner"></div></div>';

    try {
        const res = await fetch('/api/v1/workflow');
        const data = await safeJsonParse(res);

        if (data.error) {
            treeContainer.innerHTML = '<p>Error loading workflow: ' + escapeHtml(data.error) + '</p>';
            return;
        }

        // Use projects data instead of workflow
        const projects = data.projects || [];

        if (projects.length === 0) {
            treeContainer.innerHTML = `
                <div class="empty-state">
                    <i class="fas fa-folder-open"></i>
                    <p>No projects yet.</p>
                    <button class="btn btn-small btn-primary" data-action="switchView" data-id="prompt">
                        <i class="fas fa-plus"></i> Create your first project
                    </button>
                </div>`;
            return;
        }

        treeContainer.innerHTML = projects.map(project => {
            const tasks = project.tasks || [];
            const statusClass = project.status || 'pending';

            return `
                <div class="project-tree-item ${statusClass}">
                    <div class="project-tree-header" data-action="toggleProjectTasks" data-id="${escapeHtml(project.id)}">
                        <span class="tree-toggle"></span>
                        <div class="tree-content">
                            <div class="tree-label">
                                <i class="fas fa-folder"></i> ${escapeHtml(project.name)}
                                <span class="status-badge ${escapeHtml(statusClass)}">${escapeHtml(project.status)}</span>
                            </div>
                            <div class="tree-description">${escapeHtml(project.goal || project.description || 'No goal')}</div>
                        </div>
                    </div>
                    <div class="tree-children expanded" id="workflow-tasks-${escapeHtml(project.id)}">
                        ${tasks.map(task => `
                            <div class="tree-node">
                                <div class="tree-item task ${escapeHtml(task.status || 'pending')}">
                                    <span class="tree-toggle" style="visibility:hidden"></span>
                                    <div class="tree-content">
                                        <div class="tree-label">
                                            <i class="fas fa-tasks"></i> ${escapeHtml(task.id)}
                                            <span class="status-badge ${escapeHtml(task.status || 'pending')}">${escapeHtml(task.status || 'pending')}</span>
                                        </div>
                                        <div class="tree-description">${escapeHtml(task.description || '')}</div>
                                        <div class="tree-model"><i class="fas fa-microchip"></i> ${escapeHtml(task.assigned_model || 'Not assigned')}</div>
                                    </div>
                                </div>
                            </div>
                        `).join('')}
                    </div>
                </div>
            `;
        }).join('');

        // Add click handlers for toggles (guard against duplicate binding on re-render)
        document.querySelectorAll('.tree-toggle').forEach(toggle => {
            if (toggle._vetBound) return;
            toggle._vetBound = true;
            toggle.addEventListener('click', (e) => {
                e.stopPropagation();
                const parent = toggle.closest('.project-tree-item, .tree-node');
                const children = parent.querySelector('.tree-children');
                if (children) {
                    children.classList.toggle('expanded');
                    toggle.classList.toggle('expanded');
                }
            });
        });
    } catch (error) {
        treeContainer.innerHTML = '<p>Error loading workflow</p>';
    }
}

function renderWorkflowTree(node, isRoot = false) {
    const hasChildren = node.children && node.children.length > 0;
    const statusClass = node.status || '';

    let html = `
        <div class="tree-node ${isRoot ? 'tree-root' : ''}">
            <div class="tree-item ${node.type} ${statusClass}" data-id="${node.id}">
                ${hasChildren ? '<span class="tree-toggle"></span>' : '<span class="tree-toggle" style="visibility:hidden"></span>'}
                <div class="tree-content">
                    <div class="tree-label">${node.name}</div>
                    <div class="tree-description">${node.description || ''}</div>
                </div>
            </div>
    `;

    if (hasChildren) {
        html += `<div class="tree-children">`;
        for (const child of node.children) {
            html += renderWorkflowTree(child, false);
        }
        html += `</div>`;
    }

    html += `</div>`;
    return html;
}

function setupTreeToggle() {
    // Use event delegation on the parent container to avoid per-element listeners
    // that accumulate on every re-render (LEAK-03).
    const container = document.getElementById('workflowTree');
    if (!container) return;

    // Guard: only register one delegated listener per container element
    if (container._vetTreeToggleBound) return;
    container._vetTreeToggleBound = true;

    container.addEventListener('click', (e) => {
        const toggle = e.target.closest('.tree-toggle');
        if (!toggle) return;
        e.stopPropagation();
        const item = toggle.closest('.tree-item');
        if (!item) return;
        const children = item.nextElementSibling;
        if (children && children.classList.contains('tree-children')) {
            children.classList.toggle('expanded');
            toggle.classList.toggle('expanded');
        }
    });
}

// Memory View
var _memoryPage = 1;
var _memoryPerPage = 20;

async function loadMemory() {
    const list = document.getElementById('memoryList');
    if (!list) return;

    showSkeleton('memoryList', 'cards');
    if (window.ActivityTracker) ActivityTracker.startActivity('load-memory', 'Loading memory entries');

    // Load memories for browse tab
    try {
        const typeFilter = document.getElementById('memoryTypeFilter');
        const agentFilter = document.getElementById('memoryAgentFilter');
        const typeVal = typeFilter ? typeFilter.value : '';
        const agentVal = agentFilter ? agentFilter.value : '';

        let url = '/api/v1/memory?page=' + _memoryPage + '&per_page=' + _memoryPerPage;
        if (typeVal) url += '&type=' + encodeURIComponent(typeVal);
        if (agentVal) url += '&agent=' + encodeURIComponent(agentVal);

        const data = await apiCall(url);
        var memItems = (data && (data.items || data.memories || data.entries)) || [];
        if (data) {
            if (memItems.length === 0 && _memoryPage === 1) {
                list.innerHTML = `
                    <div class="empty-state">
                        <i class="fas fa-brain"></i>
                        <p>No memories yet</p>
                        <p class="empty-state-hint">Memories are created automatically as the system works, or you can add them manually.</p>
                    </div>`;
            } else {
                list.innerHTML = memItems.map(function(m) {
                    const typeClass = m.entry_type ? 'badge--' + m.entry_type.toLowerCase() : '';
                    const timeAgo = formatTimeAgo(m.timestamp);
                    return `<div class="memory-card" data-id="${escapeHtml(m.id)}">
                        <div class="memory-card__header">
                            <span class="badge ${typeClass}">${escapeHtml(m.entry_type || 'unknown')}</span>
                            <span class="memory-card__agent">${escapeHtml(m.agent || '—')}</span>
                            <span class="memory-card__time">${timeAgo}</span>
                        </div>
                        <div class="memory-card__content">${escapeHtml((m.summary || m.content || '').replace(/(\d+\.\d{2})\d{10,}/g, '$1')).substring(0, 200)}</div>
                        <div class="memory-card__actions">
                            <button class="btn btn-ghost btn-small" data-action="deleteMemory" data-id="${escapeHtml(m.id)}" title="Delete">
                                <i class="fas fa-trash"></i>
                            </button>
                        </div>
                    </div>`;
                }).join('');
            }

            // Render pagination
            renderMemoryPagination(memItems.length);
            hideSkeleton('memoryList');
            if (window.ActivityTracker) ActivityTracker.completeActivity('load-memory', `${memItems.length} entries loaded`);
        }
    } catch (err) {
        console.error('Failed to load memories:', err);
        if (window.ActivityTracker) ActivityTracker.failActivity('load-memory', 'Failed to load memories');
        showErrorState('memoryList', 'Failed to load memory entries', loadMemory);
    }

    // Load stats + populate filters + session status
    try {
        const [stats, sessionData] = await Promise.all([
            apiCall('/api/v1/memory/stats'),
            apiCall('/api/v1/memory/sessions')
        ]);

        if (stats) {
            const totalEl = document.getElementById('memoryTotalCount');
            const dbSizeEl = document.getElementById('memoryDbSize');
            const sessionEl = document.getElementById('memorySessionStatus');
            if (totalEl) totalEl.textContent = (stats.total_entries || 0).toLocaleString() + ' memories';
            if (dbSizeEl) dbSizeEl.textContent = formatBytes(stats.file_size_bytes || 0);

            // Session status from /api/v1/memory/sessions (not stats)
            if (sessionEl && sessionData) {
                const count = sessionData.entries_count || 0;
                sessionEl.textContent = sessionData.active ? 'Active (' + count + ' entries)' : 'Idle';
            } else if (sessionEl) {
                sessionEl.textContent = '—';
            }

            // Populate type filter dropdown
            populateMemoryFilter('memoryTypeFilter', stats.entries_by_type);

            // Populate agent filter dropdown
            populateMemoryFilter('memoryAgentFilter', stats.entries_by_agent);

            // By type breakdown
            const byTypeEl = document.getElementById('memoryByType');
            if (byTypeEl && stats.entries_by_type) {
                const entries = Object.entries(stats.entries_by_type);
                if (entries.length > 0) {
                    byTypeEl.innerHTML = entries.map(function(pair) {
                        return `<div class="stat-row"><span>${escapeHtml(pair[0])}</span><strong>${pair[1]}</strong></div>`;
                    }).join('');
                } else {
                    byTypeEl.innerHTML = '<p class="text-muted">No data</p>';
                }
            }

            // By agent breakdown
            const byAgentEl = document.getElementById('memoryByAgent');
            if (byAgentEl && stats.entries_by_agent) {
                const entries = Object.entries(stats.entries_by_agent);
                if (entries.length > 0) {
                    byAgentEl.innerHTML = entries.map(function(pair) {
                        return `<div class="stat-row"><span>${escapeHtml(pair[0])}</span><strong>${pair[1]}</strong></div>`;
                    }).join('');
                } else {
                    byAgentEl.innerHTML = '<p class="text-muted">No data</p>';
                }
            }
        }
    } catch (err) {
        console.error('Failed to load memory stats:', err);
    }
}

function populateMemoryFilter(selectId, dataMap) {
    const select = document.getElementById(selectId);
    if (!select || !dataMap) return;
    const currentVal = select.value;
    const allLabel = selectId === 'memoryTypeFilter' ? 'All Types' : 'All Agents';

    // Rebuild options preserving selection
    var html = '<option value="">' + allLabel + '</option>';
    var keys = Object.keys(dataMap).sort();
    for (var i = 0; i < keys.length; i++) {
        var selected = keys[i] === currentVal ? ' selected' : '';
        html += '<option value="' + escapeHtml(keys[i]) + '"' + selected + '>'
            + escapeHtml(keys[i]) + ' (' + dataMap[keys[i]] + ')</option>';
    }
    select.innerHTML = html;
}

function renderMemoryPagination(itemCount) {
    const container = document.getElementById('memoryPagination');
    if (!container) return;

    var html = '<span class="pagination-info">Page ' + _memoryPage + '</span>';

    if (_memoryPage > 1) {
        html = '<button class="btn btn-ghost btn-small" data-action="memoryPrevPage"><i class="fas fa-chevron-left"></i> Previous</button>' + html;
    }

    if (itemCount >= _memoryPerPage) {
        html += '<button class="btn btn-ghost btn-small" data-action="memoryNextPage">Next <i class="fas fa-chevron-right"></i></button>';
    }

    container.innerHTML = html;
}

function memoryNextPage() {
    _memoryPage++;
    loadMemory();
}

function memoryPrevPage() {
    if (_memoryPage > 1) _memoryPage--;
    loadMemory();
}

// Load memories filtered by type for the filter tabs
async function loadMemoryFiltered(typeFilter, tabId) {
    var listEl = document.getElementById(tabId);
    if (!listEl) return;

    var memList = listEl.querySelector('.memory-list');
    if (!memList) return;

    memList.innerHTML = '<div class="loading"><div class="spinner"></div></div>';

    // Decision tab loads from audit API instead of memory API (US-023)
    if (typeFilter === 'decision') {
        try {
            var decData = await apiCall('/api/v1/audit/decisions?limit=50');
            var decisions = (decData && decData.decisions) || [];
            if (decisions.length > 0) {
                memList.innerHTML = decisions.reverse().map(function(d) {
                    var timeAgo = formatTimeAgo(d.timestamp);
                    var badgeClass = d.decision_type === 'quality_gate' ? 'badge--warning'
                        : d.decision_type === 'model_swap' ? 'badge--danger'
                        : 'badge--info';
                    return '<div class="memory-card">' +
                        '<div class="memory-card__header">' +
                        '<span class="badge ' + badgeClass + '">' + escapeHtml(d.decision_type || '') + '</span>' +
                        '<span class="memory-card__agent">' + escapeHtml(d.choice || '') + '</span>' +
                        '<span class="memory-card__time">' + timeAgo + '</span>' +
                        '</div>' +
                        '<div class="memory-card__content">' + escapeHtml(d.reasoning || '').substring(0, 300) + '</div>' +
                        (d.alternatives && d.alternatives.length > 0
                            ? '<div class="memory-card__tags">Alternatives: ' + d.alternatives.map(function(a) { return escapeHtml(a); }).join(', ') + '</div>'
                            : '') +
                        '</div>';
                }).join('');
            } else {
                memList.innerHTML = '<div class="empty-state"><i class="fas fa-gavel"></i><p>No decisions logged yet</p></div>';
            }
        } catch (err) {
            console.error('Failed to load decisions:', err);
            memList.innerHTML = '<div class="empty-state"><i class="fas fa-exclamation-triangle"></i><p>Failed to load decisions</p></div>';
        }
        return;
    }

    try {
        var url = '/api/v1/memory?page=1&per_page=50';
        if (typeFilter) url += '&type=' + encodeURIComponent(typeFilter);

        var data = await apiCall(url);
        if (data && data.items && data.items.length > 0) {
            memList.innerHTML = data.items.map(function(m) {
                var typeClass = m.entry_type ? 'badge--' + m.entry_type.toLowerCase() : '';
                var timeAgo = formatTimeAgo(m.timestamp);
                return '<div class="memory-card" data-id="' + escapeHtml(m.id) + '">' +
                    '<div class="memory-card__header">' +
                    '<span class="badge ' + typeClass + '">' + escapeHtml(m.entry_type || 'unknown') + '</span>' +
                    '<span class="memory-card__agent">' + escapeHtml(m.agent || '\u2014') + '</span>' +
                    '<span class="memory-card__time">' + timeAgo + '</span>' +
                    '</div>' +
                    '<div class="memory-card__content">' + escapeHtml(m.summary || m.content || '').substring(0, 200) + '</div>' +
                    '<div class="memory-card__actions">' +
                    '<button class="btn btn-ghost btn-small" data-action="deleteMemory" data-id="' + escapeHtml(m.id) + '" title="Delete">' +
                    '<i class="fas fa-trash"></i></button>' +
                    '</div></div>';
            }).join('');
        } else {
            memList.innerHTML = '<div class="empty-state"><i class="fas fa-brain"></i><p>No ' + (typeFilter || '') + ' memories</p></div>';
        }
    } catch (err) {
        memList.innerHTML = '<div class="empty-state"><i class="fas fa-exclamation-triangle"></i><p>Error loading memories</p></div>';
    }
}

async function addMemoryEntry() {
    // Always use VModal for consistent UX
    var content = '';
    var entryType = 'discovery';

    if (window.VModal && VModal.prompt) {
        content = await VModal.prompt('Enter memory content:', '', 'Add Memory');
        if (!content) return;
        entryType = await VModal.prompt('Entry type:', 'discovery', 'Memory Type') || 'discovery';
    } else {
        // Fallback: create a simple inline form instead of browser prompt
        content = window.prompt('Enter memory content:');
        if (!content) return;
        entryType = 'discovery';
    }

    var result = await apiCall('/api/v1/memory', {
        method: 'POST',
        body: JSON.stringify({
            content: content,
            entry_type: entryType,
            agent: 'user',
            summary: content.substring(0, 100)
        })
    });

    if (result && result.ok) {
        if (window.ToastManager) {
            ToastManager.show('Memory stored', 'success');
        }
        _memoryPage = 1;
        loadMemory();
    }
}

function formatTimeAgo(timestamp) {
    if (!timestamp) return '—';
    const now = Date.now();
    const diff = now - timestamp;
    if (diff < 60000) return 'Just now';
    if (diff < 3600000) return Math.floor(diff / 60000) + 'm ago';
    if (diff < 86400000) return Math.floor(diff / 3600000) + 'h ago';
    if (diff < 604800000) return Math.floor(diff / 86400000) + 'd ago';
    return new Date(timestamp).toLocaleDateString();
}

function formatBytes(bytes) {
    if (bytes === 0) return '0 B';
    var sizes = ['B', 'KB', 'MB', 'GB'];
    var i = Math.floor(Math.log(bytes) / Math.log(1024));
    return VFmt.decimal(bytes / Math.pow(1024, i), 1) + ' ' + sizes[i];
}

async function searchMemory() {
    var input = document.getElementById('memorySearchInput');
    var results = document.getElementById('memorySearchResults');
    var mainList = document.getElementById('memoryList');
    if (!input || !results) return;

    // If search is empty, show main list and hide results
    if (!input.value.trim()) {
        results.style.display = 'none';
        if (mainList) mainList.style.display = '';
        return;
    }

    var query = input.value.trim();
    results.style.display = '';
    if (mainList) mainList.style.display = 'none';

    var data = await apiCall('/api/v1/memory/search?q=' + encodeURIComponent(query) + '&limit=20');
    if (data && data.items) {
        if (data.items.length === 0) {
            results.innerHTML = `
                <div class="empty-state">
                    <i class="fas fa-search"></i>
                    <p>No results for "${escapeHtml(query)}"</p>
                </div>`;
        } else {
            results.innerHTML = data.items.map(function(m) {
                var timeAgo = formatTimeAgo(m.timestamp);
                return `<div class="memory-card" data-id="${escapeHtml(m.id)}">
                    <div class="memory-card__header">
                        <span class="badge">${escapeHtml(m.entry_type || 'unknown')}</span>
                        <span class="memory-card__time">${timeAgo}</span>
                    </div>
                    <div class="memory-card__content">${escapeHtml(m.summary || m.content || '').substring(0, 300)}</div>
                </div>`;
            }).join('');
        }
    }
}

async function deleteMemory(entryId) {
    var confirmed = window.VModal
        ? await VModal.confirm('Delete this memory entry? This cannot be undone.', 'Delete Memory')
        : confirm('Delete this memory entry? This cannot be undone.');
    if (!confirmed) return;
    var result = await apiCall('/api/v1/memory/' + encodeURIComponent(entryId), {
        method: 'DELETE',
        body: JSON.stringify({ reason: 'User deleted via UI' })
    });
    if (result && result.ok) {
        if (window.ToastManager) {
            ToastManager.show('Memory deleted', 'success');
        }
        loadMemory();
    }
}

// Training
async function loadTraining() {
    showSkeleton('trainingOverview', 'list');
    if (window.ActivityTracker) ActivityTracker.startActivity('load-training', 'Loading training data');

    try {
        await Promise.all([
            loadSeedStatus(),
            loadTrainingStats()
        ]);
        hideSkeleton('trainingOverview');
        if (window.ActivityTracker) ActivityTracker.completeActivity('load-training', 'Training data loaded');
    } catch (err) {
        console.error('Failed to load training view:', err);
        if (window.ActivityTracker) ActivityTracker.failActivity('load-training', 'Failed to load training data');
        showErrorState('trainingOverview', 'Failed to load training data', loadTraining);
    }

    // Show dependency warning if training libraries are missing
    try {
        var statusRes = await apiCall('/api/v1/training/status');
        var banner = document.getElementById('trainingDepsBanner');
        if (statusRes && statusRes.ready_for_training === false) {
            var missing = (statusRes.missing_libraries || []).join(', ') || 'unsloth, trl, peft, bitsandbytes';
            if (!banner) {
                banner = document.createElement('div');
                banner.id = 'trainingDepsBanner';
                banner.className = 'alert alert-warning';
                banner.style.cssText = 'margin:1rem 0;padding:0.75rem 1rem;border-radius:8px;background:var(--warning-bg, #fff3cd);border:1px solid var(--warning-border, #ffc107);color:var(--warning-text, #856404);';
                var container = document.getElementById('trainingOverview') || document.getElementById('trainingView');
                if (container) container.prepend(banner);
            }
            banner.innerHTML = '<i class="fas fa-exclamation-triangle"></i> <strong>Training dependencies not installed.</strong> ' +
                'QLoRA fine-tuning requires GPU libraries: <code>' + missing + '</code>. ' +
                'Install with a pinned optional training environment before use. ' +
                'Data collection and curriculum planning work without these.';
            banner.style.display = '';
        } else if (banner) {
            banner.style.display = 'none';
        }
    } catch (_e) { /* non-critical */ }

    // Populate training status cards from API data
    if (statusRes) {
        var statusEl = document.getElementById('trainingStatusValue');
        if (statusEl) {
            statusEl.textContent = statusRes.is_training ? 'Training' : (statusRes.is_idle ? 'Idle' : 'Active');
        }
        var statusDetailEl = document.getElementById('trainingStatusDetail');
        if (statusDetailEl) {
            statusDetailEl.textContent = statusRes.current_activity || (statusRes.is_training ? 'Training in progress' : 'No active run');
        }
        var schedEl = document.getElementById('trainingNextScheduled');
        if (schedEl) {
            schedEl.textContent = statusRes.next_activity || 'Not scheduled';
        }
        var schedDetailEl = document.getElementById('trainingScheduleDetail');
        if (schedDetailEl && statusRes.next_activity) {
            schedDetailEl.textContent = 'Curriculum-driven';
        }
    }

    // Wire up seed data button
    const seedBtn = document.getElementById('seedDataBtn');
    if (seedBtn && !seedBtn._vetBound) {
        seedBtn._vetBound = true;
        seedBtn.addEventListener('click', seedTrainingData);
    }

    // Wire up tier select to show/hide individual model selector
    const tierSelect = document.getElementById('trainingTierSelect');
    const modelGroup = document.getElementById('trainingModelSelectGroup');
    if (tierSelect && modelGroup && !tierSelect._vetBound) {
        tierSelect._vetBound = true;
        tierSelect.addEventListener('change', () => {
            modelGroup.style.display = tierSelect.value === 'individual' ? '' : 'none';
        });
    }

    // Wire up model select to show model details and estimates
    const modelSelect = document.getElementById('trainingModelSelect');
    if (modelSelect) {
        modelSelect.addEventListener('change', function() {
            var modelId = modelSelect.value;
            var infoEl = document.getElementById('trainingModelInfo');
            if (!modelId) {
                if (infoEl) infoEl.innerHTML = '<span class="text-muted">Select a model to see details</span>';
                var vEl = document.getElementById('estimateVram');
                var dEl = document.getElementById('estimateDuration');
                var eEl = document.getElementById('estimateExamples');
                if (vEl) vEl.textContent = '—';
                if (dEl) dEl.textContent = '—';
                if (eEl) eEl.textContent = '—';
                return;
            }

            // Find the selected model in our cached models list
            var selectedModel = null;
            var allModels = (window.VApp && VApp.models) ? VApp.models : [];
            for (var i = 0; i < allModels.length; i++) {
                var m = allModels[i];
                if (m.id === modelId || m.model_id === modelId || m.name === modelId) {
                    selectedModel = m;
                    break;
                }
            }

            if (infoEl) {
                if (selectedModel) {
                    var memGb = selectedModel.memory_gb || selectedModel.size_gb || '?';
                    var ctx = selectedModel.context_length || selectedModel.context_len || 4096;
                    var caps = (selectedModel.capabilities || []).join(', ') || 'general';
                    infoEl.innerHTML =
                        '<div style="display:flex;gap:1rem;flex-wrap:wrap;font-size:0.9rem;">' +
                            '<span><i class="fas fa-memory"></i> ' + memGb + ' GB</span>' +
                            '<span><i class="fas fa-align-left"></i> ' + Number(ctx).toLocaleString() + ' ctx</span>' +
                            '<span><i class="fas fa-tags"></i> ' + escapeHtml(caps) + '</span>' +
                        '</div>';
                } else {
                    infoEl.innerHTML = '<span>' + escapeHtml(modelId) + '</span>';
                }
            }

            // Estimate VRAM needed for QLoRA fine-tuning (~1.2x model size) or full fine-tune (~4x)
            var vramEl = document.getElementById('estimateVram');
            var durationEl = document.getElementById('estimateDuration');
            var examplesEl = document.getElementById('estimateExamples');
            if (selectedModel) {
                var modelSizeGb = selectedModel.memory_gb || selectedModel.size_gb || 0;
                var vramNeeded = modelSizeGb > 0 ? VFmt.decimal(modelSizeGb * 1.2, 1) + ' GB' : '?';
                var estDuration = modelSizeGb > 0 ? Math.round(modelSizeGb * 3) + '–' + Math.round(modelSizeGb * 8) + ' min' : '?';
                var dataExamples = modelSizeGb > 0 ? (Math.round(modelSizeGb * 500)).toLocaleString() + '+' : '?';
                if (vramEl) vramEl.textContent = vramNeeded;
                if (durationEl) durationEl.textContent = estDuration;
                if (examplesEl) examplesEl.textContent = dataExamples;
            } else {
                if (vramEl) vramEl.textContent = '—';
                if (durationEl) durationEl.textContent = '—';
                if (examplesEl) examplesEl.textContent = '—';
            }
        });
    }

    // Load training history and automation rules
    loadTrainingRules();

    // Load training history
    (async function() {
        var histEl = document.getElementById('trainingHistory');
        if (!histEl) return;
        try {
            var data = await apiCall('/api/v1/training/history');
            if (data && data.runs && data.runs.length > 0) {
                histEl.innerHTML = data.runs.map(function(run) {
                    return '<div class="card" style="margin-bottom: 0.5rem; padding: 0.75rem;">' +
                        '<div style="display:flex;justify-content:space-between;align-items:center;">' +
                        '<strong>' + escapeHtml(run.run_id || '') + '</strong>' +
                        '<span class="badge ' + (run.success ? 'badge--success' : 'badge--danger') + '">' +
                        (run.success ? 'Success' : 'Failed') + '</span></div>' +
                        '<p class="text-secondary" style="margin:0.25rem 0 0;">' +
                        escapeHtml(run.base_model || '') + ' &middot; ' +
                        (run.training_examples || 0) + ' examples &middot; ' +
                        escapeHtml(run.timestamp || '') + '</p></div>';
                }).join('');
            }
        } catch (_e) { /* non-critical */ }
    })();
}

async function loadSeedStatus() {
    const el = document.getElementById('seedStatus');
    const btn = document.getElementById('seedDataBtn');
    if (!el) return;

    try {
        const res = await fetch('/api/v1/training/data/stats');
        if (!res.ok) return;
        const data = await safeJsonParse(res);

        if (data.error) {
            el.innerHTML = `<span style="color:var(--text-secondary);">${escapeHtml(data.error)}</span>`;
            return;
        }

        const seed = (data.data && data.data.seed) || data.seed_status || {};
        const downloaded = seed.downloaded || [];
        const pending = seed.pending || [];
        const total = seed.total_seed_datasets || 0;
        const examples = seed.total_examples || 0;

        if (downloaded.length === 0 && pending.length > 0) {
            // Nothing seeded yet
            el.innerHTML = `
                <div style="padding:0.75rem;background:var(--dark-tertiary);border-radius:var(--radius);border-left:3px solid var(--warning);">
                    <strong>No seed data downloaded yet.</strong>
                    <p style="color:var(--text-secondary);margin-top:0.25rem;">
                        ${total} dataset(s) available: ${pending.map(n => `<code>${n}</code>`).join(', ')}
                    </p>
                </div>
            `;
            if (btn) btn.style.display = '';
        } else if (pending.length > 0) {
            // Partially seeded
            el.innerHTML = `
                <div style="padding:0.75rem;background:var(--dark-tertiary);border-radius:var(--radius);">
                    <strong>${downloaded.length}/${total}</strong> datasets downloaded
                    (<strong>${examples.toLocaleString()}</strong> examples)
                    <div style="margin-top:0.5rem;">
                        ${downloaded.map(n => `<span class="badge badge-success" style="margin:2px;">${n}</span>`).join('')}
                        ${pending.map(n => `<span class="badge badge-warning" style="margin:2px;">${n}</span>`).join('')}
                    </div>
                </div>
            `;
            if (btn) {
                btn.innerHTML = '<i class="fas fa-download"></i> Download Remaining';
                btn.style.display = '';
            }
        } else {
            // Fully seeded
            el.innerHTML = `
                <div style="padding:0.75rem;background:var(--dark-tertiary);border-radius:var(--radius);border-left:3px solid var(--success);">
                    <strong>All ${total} datasets seeded</strong>
                    (<strong>${examples.toLocaleString()}</strong> examples)
                    <div style="margin-top:0.5rem;">
                        ${downloaded.map(n => `<span class="badge badge-success" style="margin:2px;">${n}</span>`).join('')}
                    </div>
                    <p style="color:var(--text-secondary);margin-top:0.25rem;font-size:0.85rem;">
                        Stored in: <code>${seed.data_dir || '~/.vetinari/training_data/'}</code>
                    </p>
                </div>
            `;
            if (btn) btn.style.display = 'none';
        }
    } catch {
        el.innerHTML = '<span style="color:var(--text-secondary);">Could not load seed status</span>';
    }
}

async function loadTrainingStats() {
    const statsEl = document.getElementById('trainingStats');
    if (!statsEl) return;

    try {
        const res = await fetch('/api/v1/training/data/stats');
        if (!res.ok) return;
        const data = await safeJsonParse(res);

        if (data.error) {
            statsEl.innerHTML = `<span style="color:var(--text-secondary);">${escapeHtml(data.error)}</span>`;
            return;
        }

        const collector = (data.data && data.data.collector) || data.collector_stats || {};
        const total = collector.total_examples || collector.record_count || 0;
        const byType = collector.by_type || {};

        if (total === 0) {
            statsEl.innerHTML = '<span style="color:var(--text-secondary);">No task data collected yet. Run some projects to generate training data.</span>';
        } else {
            statsEl.innerHTML = `
                <div style="display:flex;gap:1rem;flex-wrap:wrap;">
                    <span><strong>${total.toLocaleString()}</strong> examples</span>
                    ${Object.entries(byType).map(([type, count]) =>
                        `<span class="badge">${type}: ${count}</span>`
                    ).join('')}
                </div>
            `;
        }

        // Populate data quality from stats
        try {
            var qualEl = document.getElementById('trainingDataQuality');
            var qualDetailEl = document.getElementById('trainingDataDetail');
            if (qualEl) {
                var avg = collector.avg_quality || collector.average_score;
                qualEl.textContent = avg ? VFmt.pct(parseFloat(avg) * 100, 0) : '—';
            }
            if (qualDetailEl) {
                qualDetailEl.textContent = total + ' training records';
            }
        } catch (_e) {}
    } catch {
        statsEl.innerHTML = '<span style="color:var(--text-secondary);">No training data available</span>';
    }
}

function seedTrainingData() {
    const btn = document.getElementById('seedDataBtn');
    const progressEl = document.getElementById('seedProgress');
    const labelEl = document.getElementById('seedProgressLabel');
    const percentEl = document.getElementById('seedProgressPercent');
    const etaEl = document.getElementById('seedProgressEta');
    const barEl = document.getElementById('seedProgressBar');

    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Downloading...';
    }
    if (progressEl) progressEl.style.display = '';

    const source = new EventSource('/api/v1/training/data/seed/stream');

    source.onmessage = function(e) {
        var data;
        try { data = JSON.parse(e.data); } catch (_pe) { console.warn('SSE parse error:', _pe.message); return; }

        if (data.event === 'installing') {
            if (labelEl) labelEl.textContent = data.message || 'Installing packages...';
            return;
        }

        if (data.event === 'start') {
            if (labelEl) labelEl.textContent = `Downloading ${data.total} dataset(s)...`;
            if (percentEl) percentEl.textContent = '0%';
            if (barEl) barEl.style.width = '0%';
        } else if (data.event === 'progress') {
            const pct = data.percent || 0;
            if (barEl) {
                barEl.style.width = pct + '%';
                barEl.classList.toggle('failed', data.status === 'failed');
            }
            if (percentEl) percentEl.textContent = pct + '%';

            if (data.status === 'downloading') {
                if (labelEl) labelEl.textContent = `Downloading ${data.dataset} (${data.index}/${data.total})...`;
            } else if (data.status === 'complete') {
                if (labelEl) labelEl.textContent = `${data.dataset} complete (${data.examples} examples)`;
            } else if (data.status === 'failed') {
                if (labelEl) labelEl.textContent = `${data.dataset} failed: ${data.error || 'unknown'}`;
            }

            if (etaEl) {
                if (data.eta_seconds != null && data.eta_seconds > 0) {
                    etaEl.textContent = 'ETA: ' + formatEta(data.eta_seconds);
                } else {
                    etaEl.textContent = '';
                }
            }
        } else if (data.event === 'done') {
            source.close();
            if (barEl) barEl.style.width = '100%';
            if (percentEl) percentEl.textContent = '100%';
            if (etaEl) etaEl.textContent = '';
            if (labelEl) labelEl.textContent = `Done: ${data.seeded} seeded, ${data.failed} failed`;

            showStatusBanner(`Seeded ${data.seeded} dataset(s) (${data.total_examples} examples)`, 'success');

            setTimeout(() => {
                if (progressEl) progressEl.style.display = 'none';
                if (btn) {
                    btn.disabled = false;
                    btn.innerHTML = '<i class="fas fa-download"></i> Download Seed Data';
                }
                loadSeedStatus();
            }, 2000);
        } else if (data.event === 'error') {
            source.close();
            showStatusBanner('Seed error: ' + (data.error || 'unknown'), 'error');
            if (progressEl) progressEl.style.display = 'none';
            if (btn) {
                btn.disabled = false;
                btn.innerHTML = '<i class="fas fa-download"></i> Download Seed Data';
            }
        }
    };

    source.onerror = function() {
        source.close();
        showStatusBanner('Seed stream connection lost', 'error');
        if (progressEl) progressEl.style.display = 'none';
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = '<i class="fas fa-download"></i> Download Seed Data';
        }
    };
}

function formatEta(seconds) {
    if (seconds < 60) return Math.round(seconds) + 's';
    if (seconds < 3600) return Math.round(seconds / 60) + 'm ' + Math.round(seconds % 60) + 's';
    return Math.round(seconds / 3600) + 'h ' + Math.round((seconds % 3600) / 60) + 'm';
}

async function downloadModel(modelId, filename) {
    // Permission check before download
    if (typeof checkPermission === 'function') {
        const allowed = await checkPermission('ModelDownload');
        if (!allowed) return;
    }

    const progressEl = document.getElementById('modelDownloadProgress');
    const labelEl = document.getElementById('modelProgressLabel');
    const percentEl = document.getElementById('modelProgressPercent');
    const etaEl = document.getElementById('modelProgressEta');
    const barEl = document.getElementById('modelProgressBar');

    // Trigger the download
    var downloadId = modelId;
    try {
        const res = await fetch('/api/v1/models/download', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
            body: JSON.stringify({ model_id: modelId, filename: filename || '' })
        });
        const data = await safeJsonParse(res);
        if (data.error) {
            showStatusBanner('Download failed: ' + data.error, 'error');
            return;
        }
        downloadId = data.download_id || modelId;
    } catch (e) {
        showStatusBanner('Download error: ' + e.message, 'error');
        return;
    }

    // Stream progress
    if (progressEl) progressEl.style.display = '';
    if (labelEl) labelEl.textContent = `Downloading ${modelId}...`;
    if (percentEl) percentEl.textContent = '0%';
    if (barEl) barEl.style.width = '0%';

    const source = new EventSource(`/api/v1/models/download/stream?download_id=${encodeURIComponent(downloadId)}`);

    source.onmessage = function(e) {
        var data;
        try { data = JSON.parse(e.data); } catch (_pe) { console.warn('SSE parse error:', _pe.message); return; }

        if (data.event === 'progress') {
            const pct = data.percent != null ? data.percent : 0;
            const totalKnown = data.total_bytes != null && data.total_bytes > 0;
            if (barEl) {
                if (totalKnown) {
                    barEl.style.width = pct + '%';
                    barEl.classList.remove('indeterminate');
                } else {
                    // Indeterminate state: show animated bar instead of 0% or 100%
                    barEl.style.width = '';
                    barEl.classList.add('indeterminate');
                }
            }
            if (percentEl) percentEl.textContent = totalKnown ? pct + '%' : 'Downloading...';
            if (labelEl) labelEl.textContent = `Downloading ${modelId}...`;

            if (etaEl) {
                if (data.eta_seconds != null && data.eta_seconds > 0) {
                    etaEl.textContent = 'ETA: ' + formatEta(data.eta_seconds);
                } else {
                    etaEl.textContent = '';
                }
            }
        } else if (data.event === 'done') {
            source.close();
            if (barEl) barEl.style.width = '100%';
            if (percentEl) percentEl.textContent = '100%';
            if (etaEl) etaEl.textContent = '';
            if (labelEl) labelEl.textContent = `${modelId} download complete`;
            showStatusBanner(`Model ${modelId} downloaded`, 'success');

            setTimeout(() => {
                if (progressEl) progressEl.style.display = 'none';
                // Refresh models list
                if (typeof loadModels === 'function') loadModels();
            }, 2000);
        } else if (data.event === 'error') {
            source.close();
            showStatusBanner('Download error: ' + (data.error || 'unknown'), 'error');
            if (progressEl) progressEl.style.display = 'none';
        }
    };

    source.onerror = function() {
        source.close();
        if (progressEl) progressEl.style.display = 'none';
    };
}

async function exportTrainingData() {
    const tier = document.getElementById('trainingTierSelect')?.value || 'general';
    try {
        const res = await fetch('/api/training/export', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
            body: JSON.stringify({ format: 'sft', tier })
        });
        if (!res.ok) {
            showStatusBanner('Export failed: ' + (await res.text()), 'error');
            return;
        }
        const data = await safeJsonParse(res);
        if (data && data.count !== undefined) {
            showStatusBanner(`Exported ${data.count} training records`, 'success');
        } else {
            showStatusBanner('Training data exported', 'success');
        }
    } catch (e) {
        showStatusBanner('Export error: ' + e.message, 'error');
    }
}

async function startTraining() {
    // Permission check before starting training
    if (typeof checkPermission === 'function') {
        const allowed = await checkPermission('TrainingStart');
        if (!allowed) return;
    }
    const payload = buildTrainingStartPayload();
    try {
        const res = await fetch('/api/v1/training/start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
            body: JSON.stringify(payload)
        });
        const data = await safeJsonParse(res);
        if (!res.ok || data.error) {
            var errMsg = (data && data.message) || (data && data.error) || 'Training start failed';
            showStatusBanner(errMsg, 'error');
            if (window.VToast) VToast.error(errMsg);
            return;
        } else {
            showStatusBanner('Training run started', 'success');
        }
    } catch (e) {
        showStatusBanner('Training error: ' + e.message, 'error');
    }
}

// Plan Builder — template + parameter knob loader for the plan composition view
async function loadDecomposition() {
    await Promise.all([
        loadTemplates(),
        loadKnobs(),
        loadDodDorCriteria(),
        loadDecompositionHistory(),
        loadAdrList()
    ]);
    setupDecompositionEventListeners();
}

async function loadTemplates() {
    const grid = document.getElementById('templateGrid');
    grid.innerHTML = '<div class="loading"><div class="spinner"></div></div>';

    try {
        const agentFilter = document.getElementById('templateAgentFilter')?.value || '';
        const dodFilter = document.getElementById('templateDodFilter')?.value || '';

        let url = '/api/v1/decomposition/templates?';
        if (agentFilter) url += `agent_type=${agentFilter}&`;
        if (dodFilter) url += `dod_level=${dodFilter}&`;

        const res = await fetch(url);
        const data = await safeJsonParse(res);

        if (data.error) {
            grid.innerHTML = `<p>Error loading templates: ${escapeHtml(data.error)}</p>`;
            return;
        }

        if (data.templates.length === 0) {
            grid.innerHTML = '<p class="text-muted">No templates found</p>';
            return;
        }

        // Store templates for click handler to reference by index
        window._templateData = data.templates;
        grid.innerHTML = data.templates.map((t, idx) => `
            <div class="template-card" role="button" tabindex="0" aria-label="View details for ${escapeHtml(t.name)}" data-template-idx="${idx}">
                <div class="template-header">
                    <h4>${escapeHtml(t.name)}</h4>
                    <span class="badge badge-${escapeHtml(t.agent_type || '')}">${escapeHtml(t.agent_type || '')}</span>
                </div>
                ${t.subtasks ? `<p class="template-desc">${t.subtasks.length} subtasks: ${escapeHtml(t.subtasks.slice(0, 3).join(', '))}${t.subtasks.length > 3 ? '...' : ''}</p>` : ''}
                <div class="template-meta">
                    <span><i class="fas fa-layer-group"></i> DoD: ${escapeHtml(t.dod_level || 'Standard')}</span>
                    <span><i class="fas fa-tasks"></i> ${t.subtasks ? t.subtasks.length : 0} tasks</span>
                </div>
                <div class="template-keywords">
                    ${(t.keywords || []).map(k => `<span class="keyword">${escapeHtml(k)}</span>`).join('')}
                </div>
            </div>
        `).join('');
    } catch (e) {
        grid.innerHTML = `<p>Error: ${escapeHtml(e.message)}</p>`;
    }
}

/**
 * Show a modal with full details for a template card object.
 *
 * Reads name, agent_type, dod_level, subtasks, and keywords from the template
 * data and presents them in a VModal info dialog. Falls back to a plain alert
 * when VModal is unavailable.
 *
 * @param {object} t - Template data object from the /api/v1/decomposition/templates response.
 */
function showTemplateDetail(t) {
    const subtasksList = t.subtasks && t.subtasks.length
        ? t.subtasks.map(s => `<li>${escapeHtml(s)}</li>`).join('')
        : '<li class="text-muted">No subtasks defined</li>';
    const keywordBadges = (t.keywords || []).map(k => `<span class="keyword">${escapeHtml(k)}</span>`).join(' ');

    if (window.VModal) {
        const id = 'vtd_' + Date.now();
        const html = `
<div class="modal-backdrop" id="${id}_backdrop" data-modal-id="${id}">
    <div class="modal-content" style="max-width:520px;">
        <div class="modal-header">
            <h3>${escapeHtml(t.name)}</h3>
        </div>
        <div class="modal-body">
            <p><strong>Agent type:</strong> ${escapeHtml(t.agent_type || '—')}</p>
            <p><strong>DoD level:</strong> ${escapeHtml(t.dod_level || 'Standard')}</p>
            ${keywordBadges ? `<p><strong>Keywords:</strong> ${keywordBadges}</p>` : ''}
            <p><strong>Subtasks (${t.subtasks ? t.subtasks.length : 0}):</strong></p>
            <ul style="margin:0.25rem 0 0 1.25rem;padding:0;">${subtasksList}</ul>
        </div>
        <div class="modal-footer">
            <button class="btn btn-primary" data-modal-action="close" data-modal-id="${id}" data-modal-value="true">Close</button>
        </div>
    </div>
</div>`;
        VModal._openModal(id, html);
        VModal._resolvers[id] = () => {};
    } else {
        const subtasksText = t.subtasks && t.subtasks.length
            ? t.subtasks.join('\n  - ')
            : 'None';
        alert(`${t.name}\n\nAgent: ${t.agent_type || '—'}\nDoD: ${t.dod_level || 'Standard'}\nKeywords: ${(t.keywords || []).join(', ') || 'None'}\n\nSubtasks:\n  - ${subtasksText}`);
    }
}

async function loadSeedConfig() {
    const container = document.getElementById('seedConfig');
    if (!container) return;

    container.innerHTML = '<div class="loading"><div class="spinner"></div></div>';

    try {
        const res = await fetch('/api/v1/decomposition/seed-config');
        const data = await safeJsonParse(res);

        if (data.error) {
            container.innerHTML = `<p>Error: ${escapeHtml(data.error)}</p>`;
            return;
        }

        const mix = data.seed_mix || {};
        const rate = data.seed_rate || {};
        container.innerHTML = `
            <div class="seed-stats">
                <div class="seed-stat">
                    <span class="stat-label">Planner</span>
                    <span class="stat-value">${Math.round((mix.foreman || mix.oracle || 0) * 100)}%</span>
                </div>
                <div class="seed-stat">
                    <span class="stat-label">Builder</span>
                    <span class="stat-value">${Math.round((mix.worker || mix.researcher || 0) * 100)}%</span>
                </div>
                <div class="seed-stat">
                    <span class="stat-label">Reviewer</span>
                    <span class="stat-value">${Math.round((mix.inspector || mix.explorer || 0) * 100)}%</span>
                </div>
            </div>
            <div class="seed-details">
                <p><strong>Base seeds/cycle:</strong> ${rate.base || '—'}</p>
                <p><strong>Max seeds/cycle:</strong> ${rate.max || '—'}</p>
                <p><strong>Subtask cap:</strong> ${rate.subtask_min || '—'}-${rate.subtask_max || '—'}</p>
                <p><strong>Depth range:</strong> ${data.min_max_depth || '—'}-${data.max_max_depth || '—'} (default: ${data.default_max_depth || '—'})</p>
            </div>
        `;
    } catch (e) {
        container.innerHTML = `<p>Error: ${escapeHtml(e.message)}</p>`;
    }
}

async function loadDodDorCriteria() {
    const dodLevel = document.getElementById('dodLevelSelect')?.value || 'Standard';
    const dorLevel = document.getElementById('dorLevelSelect')?.value || 'Standard';

    try {
        const [dodRes, dorRes] = await Promise.all([
            fetch(`/api/v1/decomposition/dod-dor?level=${dodLevel}`),
            fetch(`/api/v1/decomposition/dod-dor?level=${dorLevel}`)
        ]);

        const dodData = await safeJsonParse(dodRes);
        const dorData = await safeJsonParse(dorRes);

        const dodCriteria = document.getElementById('dodCriteria');
        const dorCriteria = document.getElementById('dorCriteria');

        if (dodCriteria) {
            dodCriteria.innerHTML = dodData.dod_criteria?.map(c => `<li>${escapeHtml(c)}</li>`).join('') || '';
        }
        if (dorCriteria) {
            dorCriteria.innerHTML = dorData.dor_criteria?.map(c => `<li>${escapeHtml(c)}</li>`).join('') || '';
        }
    } catch (e) {
        console.error('Error loading DoD/DoR criteria:', e);
    }
}

async function loadDecompositionHistory() {
    const list = document.getElementById('historyList');
    if (!list) return;

    list.innerHTML = '<div class="loading"><div class="spinner"></div></div>';

    try {
        const planFilter = document.getElementById('historyPlanFilter')?.value || '';
        let url = '/api/v1/decomposition/history?';
        if (planFilter) url += `plan_id=${planFilter}`;

        const res = await fetch(url);
        const data = await safeJsonParse(res);

        if (data.error) {
            list.innerHTML = `<p>Error: ${escapeHtml(data.error)}</p>`;
            return;
        }

        if (data.history.length === 0) {
            list.innerHTML = '<p class="text-muted">No decomposition history yet</p>';
            return;
        }

        list.innerHTML = data.history.map(h => `
            <div class="history-item">
                <div class="history-header">
                    <span class="history-id">${h.event_id}</span>
                    <span class="history-time">${new Date(h.timestamp).toLocaleString()}</span>
                </div>
                <div class="history-details">
                    <span>Task: ${h.task_id}</span>
                    <span>Depth: ${h.depth}</span>
                    <span>Subtasks: ${h.subtasks_created}</span>
                </div>
                <div class="history-seeds">
                    Seeds: ${h.seeds_used.map(s => `<span class="seed-badge">${s}</span>`).join('')}
                </div>
            </div>
        `).join('');
    } catch (e) {
        list.innerHTML = `<p>Error: ${escapeHtml(e.message)}</p>`;
    }
}

async function loadAdrList() {
    const list = document.getElementById('adrList');
    if (!list) return;

    list.innerHTML = '<div class="loading"><div class="spinner"></div></div>';

    try {
        const res = await fetch('/api/adr');
        const data = await safeJsonParse(res);

        if (data.error) {
            list.innerHTML = `<p>Error: ${escapeHtml(data.error)}</p>`;
            return;
        }

        if (data.adrs.length === 0) {
            list.innerHTML = '<p class="text-muted">No ADR records yet</p>';
            return;
        }

        list.innerHTML = data.adrs.map(a => `
            <div class="adr-item" data-adr-id="${a.adr_id}">
                <div class="adr-header">
                    <span class="adr-id">${a.adr_id}</span>
                    <span class="adr-status ${a.status}">${a.status}</span>
                    <span class="adr-category">${a.category}</span>
                </div>
                <h4 class="adr-title">${a.title}</h4>
                <p class="adr-context">${a.context.substring(0, 100)}${a.context.length > 100 ? '...' : ''}</p>
            </div>
        `).join('');
    } catch (e) {
        list.innerHTML = `<p>Error: ${escapeHtml(e.message)}</p>`;
    }
}

async function decomposeWithAgent() {
    const prompt = document.getElementById('decomposePrompt').value;
    const planId = document.getElementById('decomposePlanId').value || 'default';

    if (!prompt.trim()) {
        showStatusBanner('Please enter a task prompt', 'error');
        return;
    }

    const btn = document.getElementById('decomposeAgentBtn');
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<div class="spinner"></div> Decomposing...';
    }

    try {
        const res = await fetch('/api/v1/decomposition/decompose-agent', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
            body: JSON.stringify({
                plan_id: planId,
                prompt: prompt
            })
        });

        const data = await safeJsonParse(res);

        if (data.error) {
            showStatusBanner(data.error, 'error');
            return;
        }

        document.getElementById('subtaskCount').textContent = data.count;

        const tree = document.getElementById('subtaskTree');
        if (data.subtasks.length === 0) {
            tree.innerHTML = '<p class="text-muted">No subtasks generated</p>';
        } else {
            tree.innerHTML = renderSubtaskTree(data.subtasks, data.plan_id);
        }

        showStatusBanner(`Generated ${data.count} subtasks (depth: ${data.depth})`, 'success');

        loadDecompositionHistory();
    } catch (e) {
        showStatusBanner(e.message, 'error');
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = '<i class="fas fa-robot"></i> Decompose with Agent';
        }
    }
}

function renderSubtaskTree(subtasks, planId) {
    const byParent = {};
    subtasks.forEach(st => {
        if (!byParent[st.parent_id]) byParent[st.parent_id] = [];
        byParent[st.parent_id].push(st);
    });

    function renderNode(parentId, depth = 0) {
        const children = byParent[parentId] || [];
        if (children.length === 0 && depth > 0) return '';

        return children.map(st => `
            <div class="subtask-item" style="margin-left: ${depth * 20}px" data-subtask-id="${escapeHtml(st.subtask_id)}">
                <div class="subtask-header">
                    <span class="subtask-id">${escapeHtml(st.subtask_id)}</span>
                    <span class="badge badge-${escapeHtml(st.agent_type)}">${escapeHtml(st.agent_type)}</span>
                    <span class="depth-badge">D:${st.depth}</span>
                    <span class="subtask-dod">DoD: ${escapeHtml(st.dod_level)}</span>
                </div>
                <div class="subtask-desc">${escapeHtml(st.description)}</div>
                <div class="subtask-assigned">
                    ${st.assigned_agent ? `<span class="assigned-to">Assigned: ${escapeHtml(st.assigned_agent)}</span>` : '<span class="unassigned">Unassigned</span>'}
                </div>
                ${renderNode(st.subtask_id, depth + 1)}
            </div>
        `).join('');
    }

    return renderNode('root');
}

async function runAssignmentPass() {
    const planId = document.getElementById('decomposePlanId').value || 'default';

    const btn = document.getElementById('runAssignmentBtn');
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<div class="spinner"></div> Assigning...';
    }

    try {
        const res = await fetch('/api/v1/assignments/execute-pass', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
            body: JSON.stringify({
                plan_id: planId,
                auto_assign: true
            })
        });

        const data = await safeJsonParse(res);

        if (data.error) {
            showStatusBanner(data.error, 'error');
            return;
        }

        showStatusBanner(`Assigned ${data.assigned} subtasks, ${data.unassigned} unassigned`, 'success');

        loadSubtaskTree(planId);
    } catch (e) {
        showStatusBanner(e.message, 'error');
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = '<i class="fas fa-users"></i> Run Assignment Pass';
        }
    }
}

async function loadSubtaskTree(planId) {
    try {
        const res = await fetch(`/api/v1/subtasks/${planId}/tree`);
        const data = await safeJsonParse(res);

        if (data.error) {
            console.error(data.error);
            return;
        }

        const tree = document.getElementById('subtaskTree');
        if (data.subtasks && data.subtasks.length > 0) {
            tree.innerHTML = renderSubtaskTree(data.subtasks, planId);
            document.getElementById('subtaskCount').textContent = data.total;
        }
    } catch (e) {
        console.error('Error loading subtask tree:', e);
    }
}

async function loadKnobs() {
    try {
        const res = await fetch('/api/v1/decomposition/knobs');
        const data = await safeJsonParse(res);

        const container = document.getElementById('seedConfig');
        if (container && !data.error) {
            const mix = data.seed_mix || {};
            const rate = data.seed_rate || {};
            const knobs = data.recursion_knobs || {};
            container.innerHTML = `
                <div class="seed-stats">
                    <div class="seed-stat">
                        <span class="stat-label">Planner</span>
                        <span class="stat-value">${Math.round((mix.foreman || mix.oracle || 0) * 100)}%</span>
                    </div>
                    <div class="seed-stat">
                        <span class="stat-label">Builder</span>
                        <span class="stat-value">${Math.round((mix.worker || mix.researcher || 0) * 100)}%</span>
                    </div>
                    <div class="seed-stat">
                        <span class="stat-label">Reviewer</span>
                        <span class="stat-value">${Math.round((mix.inspector || mix.explorer || 0) * 100)}%</span>
                    </div>
                </div>
                <div class="seed-details">
                    <p><strong>Base seeds/cycle:</strong> ${rate.base || '—'}</p>
                    <p><strong>Max seeds/cycle:</strong> ${rate.max || '—'}</p>
                    <p><strong>Subtask cap:</strong> ${rate.subtask_min || '—'}-${rate.subtask_max || '—'}</p>
                    <p><strong>Depth range:</strong> ${data.min_max_depth || '—'}-${data.max_max_depth || '—'} (default: ${data.default_max_depth || '—'})</p>
                    <p><strong>Effort threshold:</strong> ${knobs.est_effort_threshold || '—'}</p>
                </div>
            `;
        }
    } catch (e) {
        console.error('Error loading knobs:', e);
    }
}

let _decompositionListenersReady = false;
function setupDecompositionEventListeners() {
    // Guard: only bind once to prevent listener stacking on repeated view switches
    if (_decompositionListenersReady) return;
    _decompositionListenersReady = true;

    // Tab switching (container is #decompositionView .content, not .decomposition-container)
    document.querySelectorAll('#decompositionView .tab[data-tab]').forEach(tab => {
        tab.addEventListener('click', () => {
            document.querySelectorAll('#decompositionView .tab[data-tab]').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('#decompositionView .tab-content').forEach(c => c.classList.remove('active'));
            tab.classList.add('active');
            document.getElementById(`tab-${tab.dataset.tab}`).classList.add('active');
        });
    });

    // Template card click — show detail modal (uses index into window._templateData)
    function _handleTemplateClick(e) {
        const card = e.target.closest('.template-card');
        if (!card) return;
        const idx = parseInt(card.dataset.templateIdx, 10);
        if (isNaN(idx) || !window._templateData || !window._templateData[idx]) return;
        if (e.type === 'keydown') e.preventDefault();
        showTemplateDetail(window._templateData[idx]);
    }
    document.getElementById('templateGrid')?.addEventListener('click', _handleTemplateClick);
    document.getElementById('templateGrid')?.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') _handleTemplateClick(e);
    });

    // Template filters
    document.getElementById('templateAgentFilter')?.addEventListener('change', loadTemplates);
    document.getElementById('templateDodFilter')?.addEventListener('change', loadTemplates);

    // DoD/DoR level selectors
    document.getElementById('dodLevelSelect')?.addEventListener('change', loadDodDorCriteria);
    document.getElementById('dorLevelSelect')?.addEventListener('change', loadDodDorCriteria);

    // Decompose button
    document.getElementById('decomposeBtn')?.addEventListener('click', async () => {
        const prompt = document.getElementById('decomposePrompt').value;
        const depth = parseInt(document.getElementById('decomposeDepth').value) || 0;
        const maxDepth = parseInt(document.getElementById('decomposeMaxDepth').value) || 14;
        const planId = document.getElementById('decomposePlanId').value || 'default';

        if (!prompt.trim()) {
            showStatusBanner('Please enter a task prompt', 'error');
            return;
        }

        const btn = document.getElementById('decomposeBtn');
        btn.disabled = true;
        btn.innerHTML = '<div class="spinner"></div> Decomposing...';

        try {
            const res = await fetch('/api/v1/decomposition/decompose', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
                body: JSON.stringify({
                    task_prompt: prompt,
                    parent_task_id: 'root',
                    depth: depth,
                    max_depth: maxDepth,
                    plan_id: planId
                })
            });

            const data = await safeJsonParse(res);

            if (data.error) {
                showStatusBanner(data.error, 'error');
                return;
            }

            document.getElementById('subtaskCount').textContent = data.count;

            const tree = document.getElementById('subtaskTree');
            if (data.subtasks.length === 0) {
                tree.innerHTML = '<p class="text-muted">No subtasks generated (max depth reached)</p>';
            } else {
                tree.innerHTML = data.subtasks.map(st => `
                    <div class="subtask-item" style="margin-left: ${(st.depth || 1) * 20}px">
                        <div class="subtask-header">
                            <span class="subtask-id">${st.task_id}</span>
                            <span class="badge badge-${st.agent_type}">${st.agent_type}</span>
                            <span class="subtask-dod">DoD: ${st.dod_level}</span>
                        </div>
                        <div class="subtask-desc">${st.description}</div>
                        <div class="subtask-prompt">${st.prompt.substring(0, 80)}...</div>
                    </div>
                `).join('');
            }

            showStatusBanner(`Generated ${data.count} subtasks`, 'success');

            // Refresh history
            loadDecompositionHistory();

        } catch (e) {
            showStatusBanner(e.message, 'error');
        } finally {
            btn.disabled = false;
            btn.innerHTML = '<i class="fas fa-sitemap"></i> Decompose';
        }
    });

    // History refresh
    document.getElementById('refreshHistoryBtn')?.addEventListener('click', loadDecompositionHistory);
    document.getElementById('historyPlanFilter')?.addEventListener('input', debounce(loadDecompositionHistory, 300));
}

// Models
async function loadModels() {
    const grid = document.getElementById('modelsGrid');
    if (!grid) return;
    showSkeleton('modelsGrid', 'cards');
    if (window.ActivityTracker) ActivityTracker.startActivity('load-models', 'Loading models');

    // Wire the Refresh button to force-bypass the cache
    const refreshBtn = document.getElementById('refreshModels');
    if (refreshBtn && !refreshBtn._wired) {
        refreshBtn._wired = true;
        refreshBtn.addEventListener('click', async () => {
            refreshBtn.disabled = true;
            refreshBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';
            try {
                await fetch('/api/v1/models/refresh', { method: 'POST', headers: {'X-Requested-With': 'XMLHttpRequest'} });
                await loadModels();
            } catch (e) {
                if (window.VToast) VToast.error('Model refresh failed');
            } finally {
                refreshBtn.disabled = false;
                refreshBtn.innerHTML = '<i class="fas fa-sync"></i> Refresh';
            }
        });
    }

    try {
        const res = await fetch('/api/v1/models');
        const data = await safeJsonParse(res);

        if (data.error) {
            grid.innerHTML = `<div class="empty-state"><i class="fas fa-exclamation-triangle"></i><p>Error: ${escapeHtml(data.error)}</p><button class="btn btn-small btn-primary" data-action="loadModels">Retry</button></div>`;
            return;
        }

        const modelList = data.models || [];
        _lastModelCount = modelList.length;

        if (modelList.length === 0) {
            if (window.ActivityTracker) ActivityTracker.completeActivity('load-models', 'No models found');
            grid.innerHTML = `
                <div class="empty-state">
                    <i class="fas fa-microchip"></i>
                    <p>No models found. Place <span class="vt-tooltip" data-tooltip="GGUF (GPT-Generated Unified Format) — a compact model file format optimized for fast local inference on consumer hardware">GGUF</span> files in the models directory and click Discover.</p>
                    <button class="btn btn-small btn-primary" data-action="discoverBtnProxy">
                        <i class="fas fa-compass"></i> Discover Models
                    </button>
                </div>`;
            return;
        }

        hideSkeleton('modelsGrid');
        if (window.ActivityTracker) ActivityTracker.completeActivity('load-models', modelList.length + ' models loaded');
        grid.innerHTML = modelList.map(model => {
            const source = model.source || (model.id && model.id.startsWith('cloud:') ? 'cloud' : 'local');
            const sourceBadge = source === 'cloud'
                ? '<span class="model-badge badge--cloud">Cloud</span>'
                : '<span class="model-badge badge--local">Local</span>';
            const modelKey = model.name || model.id || '';
            const isFavorite = window._favoriteModel && modelKey.toLowerCase().includes(window._favoriteModel.toLowerCase());
            const favBadge = isFavorite ? '<span class="model-card__favorite-badge"><i class="fas fa-heart"></i> Your favorite</span>' : '';
            return `
            <div class="model-card${isFavorite ? ' model-card--favorite' : ''}" lang="en">
                <div class="model-card-header">
                    <span class="model-name" title="${model.id || model.name}">${model.name || model.id}</span>
                    ${sourceBadge}
                    ${model.version ? `<span class="model-badge">${model.version}</span>` : ''}
                    ${favBadge}
                </div>
                <div class="model-info">
                    ${model.memory_gb ? `<span><i class="fas fa-memory"></i> ${model.memory_gb} GB</span>` : ''}
                    ${model.context_len ? `<span><i class="fas fa-align-left"></i> ${model.context_len.toLocaleString()} ctx</span>` : ''}
                    ${(model.capabilities || []).length ? `<span><i class="fas fa-cogs"></i> ${model.capabilities.join(', ')}</span>` : ''}
                </div>
            </div>`;
        }).join('');

    } catch (error) {
        console.error('loadModels error:', error);
        if (window.ActivityTracker) ActivityTracker.failActivity('load-models', 'Failed to load models');
        showErrorState('modelsGrid', 'Failed to load models', loadModels);
    }
}

// Online model search
function wireModelSearch() {
    const btn = document.getElementById('modelSearchBtn');
    const input = document.getElementById('modelSearchInput');
    if (!btn || !input) return;

    async function doSearch() {
        const query = input.value.trim();
        if (!query) return;

        const results = document.getElementById('modelSearchResults');
        results.innerHTML = '<div class="loading"><div class="spinner"></div><p style="margin-top:8px;color:var(--text-muted);font-size:0.8rem;">Searching online...</p></div>';
        btn.disabled = true;

        try {
            const res = await fetch('/api/v1/models/search', {
                method: 'POST',
                headers: {'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest'},
                body: JSON.stringify({query})
            });
            const data = await safeJsonParse(res);

            if (data.error) {
                results.innerHTML = `<p style="color:var(--error);">Error: ${escapeHtml(data.error)}</p>`;
                return;
            }

            const candidates = data.candidates || [];
            if (candidates.length === 0) {
                results.innerHTML = '<p style="color:var(--text-muted);">No models found for that query.</p>';
                return;
            }

            results.innerHTML = candidates.map((c, i) => `
                <div class="model-card model-card--compact">
                    <div class="model-card-header">
                        <span class="model-candidate-rank">#${i + 1}</span>
                        <span class="model-name" title="${escapeHtml(c.id)}">${escapeHtml(c.name || c.id)}</span>
                        <span class="model-badge">${escapeHtml(c.source_type || 'unknown')}</span>
                        <span class="model-badge badge--score">${VFmt.decimal(c.final_score || 0, 2)}</span>
                    </div>
                    <div class="model-info">
                        ${c.memory_gb ? `<span><i class="fas fa-memory"></i> ${c.memory_gb} GB</span>` : ''}
                        ${c.context_len ? `<span><i class="fas fa-align-left"></i> ${c.context_len.toLocaleString()} ctx</span>` : ''}
                        ${c.short_rationale ? `<span style="color:var(--text-muted);font-size:0.75rem;">${escapeHtml(c.short_rationale)}</span>` : ''}
                    </div>
                    <div style="margin-top:4px;display:flex;gap:0.5rem;align-items:center;">
                        ${c.provenance && c.provenance.length > 0 && c.provenance[0].url ? `<a href="${escapeHtml(c.provenance[0].url)}" target="_blank" rel="noopener noreferrer" style="color:var(--primary-light);font-size:0.75rem;">View Source</a>` : ''}
                        ${c.id ? `<button class="btn btn-small btn-primary" data-action="downloadModel" data-id="${escapeHtml(c.id)}" data-arg2="${escapeHtml(c.filename || '')}"><i class="fas fa-download"></i> Download</button>` : ''}
                    </div>
                </div>
            `).join('');
        } catch (error) {
            results.innerHTML = `<p style="color:var(--error);">Search error: ${escapeHtml(error.message)}</p>`;
        } finally {
            btn.disabled = false;
        }
    }

    btn.addEventListener('click', doSearch);
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') doSearch(); });
}

// Models — Recommended tab
async function loadModelsRecommended() {
    var grid = document.getElementById('modelsRecommendedGrid');
    if (!grid) return;
    grid.innerHTML = '<div class="loading"><div class="spinner"></div></div>';

    try {
        var res = await fetch('/api/v1/models/recommendations');
        var data = await safeJsonParse(res);
        var recs = data.recommendations || [];

        if (recs.length === 0) {
            grid.innerHTML = '<div class="empty-state"><i class="fas fa-star"></i><p>No recommendations available</p><p class="empty-state-hint">Recommendations are generated based on your hardware profile and usage patterns.</p></div>';
            return;
        }

        grid.innerHTML = recs.map(function(r) {
            return '<div class="model-card">' +
                '<div class="model-card-header">' +
                '<span class="model-name">' + escapeHtml(r.name || r.id || '') + '</span>' +
                '<span class="model-badge badge--recommended">Recommended</span>' +
                '</div>' +
                '<div class="model-info">' +
                (r.memory_gb ? '<span><i class="fas fa-memory"></i> ' + r.memory_gb + ' GB</span>' : '') +
                (r.reason ? '<span>' + escapeHtml(r.reason) + '</span>' : '') +
                '</div>' +
                (r.id ? '<div class="model-card-actions"><button class="btn btn-small btn-primary" data-action="downloadModel" data-id="' + escapeHtml(r.id) + '"><i class="fas fa-download"></i> Download</button></div>' : '') +
                '</div>';
        }).join('');
    } catch (error) {
        grid.innerHTML = '<div class="empty-state"><i class="fas fa-star"></i><p>Could not load recommendations</p></div>';
    }
}

// Dashboard — GPU stats
async function loadGpuStats() {
    try {
        var res = await fetch('/api/v1/system/gpu');
        var data = await safeJsonParse(res);

        var nameEl = document.getElementById('gpuName');
        var vramBar = document.getElementById('gpuVramBar');
        var vramText = document.getElementById('gpuVramText');
        var utilEl = document.getElementById('gpuUtilization');
        var tempEl = document.getElementById('gpuTemperature');

        if (!data.gpu_available || !data.gpus || data.gpus.length === 0) {
            if (nameEl) nameEl.textContent = 'No GPU detected';
            return;
        }

        var gpu = data.gpus[0];
        if (nameEl) nameEl.textContent = gpu.name || 'Unknown GPU';
        if (vramBar) {
            var pct = gpu.vram_used_percent || 0;
            vramBar.style.width = pct + '%';
            vramBar.className = 'hw-usage-bar__fill ' + (pct > 80 ? 'usage-high' : pct > 50 ? 'usage-medium' : 'usage-low');
        }
        if (vramText) vramText.textContent = VFmt.decimal((gpu.vram_used_mb || 0) / 1024, 1) + ' / ' + VFmt.decimal((gpu.vram_total_mb || 0) / 1024, 1) + ' GB';
        if (utilEl) utilEl.textContent = (gpu.utilization_percent || 0) + '%';
        if (tempEl) tempEl.textContent = gpu.temperature_c != null ? gpu.temperature_c + '°C' : '—';
    } catch {
        // Silently ignore — GPU monitoring is optional
    }
}

// Dashboard — System resources
async function loadSystemResources() {
    try {
        var res = await fetch('/api/v1/system/resources');
        var data = await safeJsonParse(res);

        // RAM
        var ramBar = document.getElementById('ramUsageBar');
        var ramText = document.getElementById('ramUsageText');
        if (ramBar && data.ram) {
            ramBar.style.width = data.ram.percent + '%';
            ramBar.className = 'hw-usage-bar__fill ' + (data.ram.percent > 80 ? 'usage-high' : data.ram.percent > 50 ? 'usage-medium' : 'usage-low');
        }
        if (ramText && data.ram) ramText.textContent = VFmt.decimal(data.ram.used_mb / 1024, 1) + ' / ' + VFmt.decimal(data.ram.total_mb / 1024, 1) + ' GB';

        // CPU
        var cpuBar = document.getElementById('cpuUsageBar');
        var cpuText = document.getElementById('cpuUsageText');
        if (cpuBar && data.cpu) {
            cpuBar.style.width = data.cpu.percent + '%';
            cpuBar.className = 'hw-usage-bar__fill ' + (data.cpu.percent > 80 ? 'usage-high' : data.cpu.percent > 50 ? 'usage-medium' : 'usage-low');
        }
        if (cpuText && data.cpu) cpuText.textContent = data.cpu.percent + '% (' + data.cpu.cores + ' cores)';

        // Disk
        var diskBar = document.getElementById('diskUsageBar');
        var diskText = document.getElementById('diskUsageText');
        if (diskBar && data.disk) {
            diskBar.style.width = data.disk.percent + '%';
            diskBar.className = 'hw-usage-bar__fill ' + (data.disk.percent > 80 ? 'usage-high' : data.disk.percent > 50 ? 'usage-medium' : 'usage-low');
        }
        if (diskText && data.disk) diskText.textContent = VFmt.decimal(data.disk.used_gb, 1) + ' / ' + VFmt.decimal(data.disk.total_gb, 1) + ' GB';
    } catch {
        // Silently ignore — system monitoring is optional
    }
}

// ── Phase 4: Training Quick Actions ────────────────────────────────────────

function quickStartTraining() {
    // Switch to Train tab and focus the start button
    var trainTab = document.querySelector('[data-tab="training-train"]');
    if (trainTab) trainTab.click();
}

async function quickCollectData() {
    try {
        var res = await apiCall('/api/v1/training/data/seed', { method: 'POST' });
        if (res && !res.error) {
            if (res.count_seeded > 0) {
                if (window.VToast) VToast.success('Seeded ' + res.count_seeded + ' dataset(s)');
            } else {
                if (window.VToast) VToast.info('No new data to seed — datasets library may not be installed in the active environment');
            }
        } else {
            if (window.VToast) VToast.error('Failed to start data collection: ' + (res.error || 'Unknown error'));
        }
    } catch (err) {
        if (window.VToast) VToast.error('Data collection failed');
    }
}

function quickViewLastRun() {
    // Switch to Experiments tab
    var expTab = document.querySelector('[data-tab="training-experiments"]');
    if (expTab) expTab.click();
}

// ── Phase 4: Training Config & Monitor ─────────────────────────────────────

function saveTrainingConfig() {
    var config = {
        method: document.querySelector('#trainingMethodSelector .pill.active')?.dataset.value || 'qlora',
        learningRate: document.getElementById('trainingLearningRate')?.value || '2e-4',
        epochs: document.getElementById('trainingEpochs')?.value || '3',
        batchSize: document.getElementById('trainingBatchSize')?.value || '4',
        loraRank: document.querySelector('#loraRankSelector .pill.active')?.dataset.value || '16',
        loraAlpha: document.getElementById('trainingLoraAlpha')?.value || '32',
        tier: document.getElementById('trainingTierSelect')?.value || 'general',
        trainValSplit: document.getElementById('trainValSplit')?.value || '90',
        gradAccum: document.getElementById('trainingGradAccum')?.value || '4',
        weightDecay: document.getElementById('trainingWeightDecay')?.value || '0.01',
        maxSeqLen: document.getElementById('trainingMaxSeqLen')?.value || '2048',
        scheduler: document.getElementById('trainingScheduler')?.value || 'cosine',
    };
    try {
        localStorage.setItem('vetinari_training_config', JSON.stringify(config));
        if (window.VToast) VToast.success('Training configuration saved');
    } catch (e) {
        if (window.VToast) VToast.error('Failed to save configuration');
    }
}

function getSavedTrainingConfig() {
    try {
        var raw = localStorage.getItem('vetinari_training_config');
        if (!raw) return {};
        var parsed = JSON.parse(raw);
        return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {};
    } catch (e) {
        return {};
    }
}

function buildTrainingStartPayload(extra) {
    var saved = getSavedTrainingConfig();
    var tier = document.getElementById('trainingTierSelect')?.value || saved.tier || 'general';
    var model = document.getElementById('trainingModelSelect')?.value || '';
    var payload = Object.assign({}, extra || {}, {
        tier: tier,
        config: {
            method: saved.method || document.querySelector('#trainingMethodSelector .pill.active')?.dataset.value || 'qlora',
            learning_rate: saved.learningRate || document.getElementById('trainingLearningRate')?.value || '2e-4',
            epochs: saved.epochs || document.getElementById('trainingEpochs')?.value || '3',
            batch_size: saved.batchSize || document.getElementById('trainingBatchSize')?.value || '4',
            lora_rank: saved.loraRank || document.querySelector('#loraRankSelector .pill.active')?.dataset.value || '16',
            lora_alpha: saved.loraAlpha || document.getElementById('trainingLoraAlpha')?.value || '32',
            train_val_split: saved.trainValSplit || document.getElementById('trainValSplit')?.value || '90',
            grad_accum: saved.gradAccum || document.getElementById('trainingGradAccum')?.value || '4',
            weight_decay: saved.weightDecay || document.getElementById('trainingWeightDecay')?.value || '0.01',
            max_seq_len: saved.maxSeqLen || document.getElementById('trainingMaxSeqLen')?.value || '2048',
            scheduler: saved.scheduler || document.getElementById('trainingScheduler')?.value || 'cosine',
        }
    });
    if (tier === 'individual' && model) {
        payload.model_id = model;
    }
    return payload;
}

async function dryRunTraining() {
    if (window.VToast) VToast.info('Validating training configuration...');
    try {
        var model = document.getElementById('trainingModelSelect')?.value;
        if (!model) {
            if (window.VToast) VToast.warning('Select a model before running validation');
            return;
        }
        // Use the status endpoint to validate the config is sane
        var res = await apiCall('/api/v1/training/status');
        if (res && !res.error) {
            if (window.VToast) VToast.success('Configuration is valid. Ready to train.');
        }
    } catch (err) {
        if (window.VToast) VToast.error('Validation failed: ' + (err.message || 'Unknown error'));
    }
}

async function pauseTraining() {
    try {
        var res = await apiCall('/api/v1/training/pause', { method: 'POST' });
        if (res && !res.error) {
            if (window.VToast) VToast.info('Training paused');
        }
    } catch (err) {
        if (window.VToast) VToast.error('Failed to pause training');
    }
}

async function stopTraining() {
    var confirmed = true;
    if (window.VModal && VModal.confirm) {
        confirmed = await VModal.confirm('Stop the current training run? Progress will be lost.');
    }
    if (!confirmed) return;
    try {
        var res = await apiCall('/api/v1/training/stop', { method: 'POST' });
        if (res && !res.error) {
            if (window.VToast) VToast.info('Training stopped');
            var monitor = document.getElementById('trainingMonitor');
            if (monitor) monitor.style.display = 'none';
        }
    } catch (err) {
        if (window.VToast) VToast.error('Failed to stop training');
    }
}

// ── Phase 4: Training Data ─────────────────────────────────────────────────

async function filterTrainingData() {
    var typeFilter = document.getElementById('trainingDataTypeFilter')?.value || '';
    var minQuality = document.getElementById('trainingMinQuality')?.value || '0';
    var table = document.getElementById('trainingDataBrowser');
    if (!table) return;

    table.innerHTML = '<div class="loading"><div class="spinner"></div></div>';

    try {
        var url = '/api/v1/training/data/browse?page=1&per_page=50';
        if (typeFilter) url += '&type=' + encodeURIComponent(typeFilter);
        if (parseInt(minQuality) > 0) url += '&min_quality=' + (parseInt(minQuality) / 100).toFixed(2);

        var data = await apiCall(url);
        if (data && data.items && data.items.length > 0) {
            var html = '<table class="data-table"><thead><tr>' +
                '<th>Type</th><th>Quality</th><th>Source</th><th>Date</th>' +
                '</tr></thead><tbody>';
            data.items.forEach(function(item) {
                html += '<tr>' +
                    '<td>' + escapeHtml(item.type || '—') + '</td>' +
                    '<td>' + (item.quality != null ? VFmt.pct(parseFloat(item.quality) * 100, 0) : '—') + '</td>' +
                    '<td>' + escapeHtml(item.source || '—') + '</td>' +
                    '<td>' + escapeHtml(item.date || '—') + '</td>' +
                    '</tr>';
            });
            html += '</tbody></table>';
            html += '<p class="text-secondary">' + data.total + ' items found</p>';
            table.innerHTML = html;
        } else {
            table.innerHTML = '<div class="empty-state"><i class="fas fa-database"></i><p>No training data matches the filters</p></div>';
        }
    } catch (err) {
        table.innerHTML = '<div class="empty-state"><i class="fas fa-exclamation-triangle"></i><p>Error loading data</p></div>';
    }
}

async function generateSyntheticData() {
    if (window.VToast) VToast.info('Starting synthetic data generation...');
    try {
        var res = await apiCall('/api/v1/training/data/seed', {
            method: 'POST',
            body: JSON.stringify({ source: 'synthetic' })
        });
        if (res && !res.error) {
            if (window.VToast) VToast.success('Synthetic data generation started');
        } else {
            if (window.VToast) VToast.error('Generation failed: ' + (res.error || 'Unknown error'));
        }
    } catch (err) {
        if (window.VToast) VToast.error('Synthetic data generation failed');
    }
}

// ── Phase 4: Training Automation ───────────────────────────────────────────

async function addTrainingRule() {
    var ruleName = '';
    var ruleCondition = '';

    if (window.VModal && VModal.prompt) {
        ruleName = await VModal.prompt('Rule name (e.g. "Weekly retraining"):');
        if (!ruleName) return;
        ruleCondition = await VModal.prompt('Condition (e.g. "quality drops below 0.7 for coding"):');
        if (!ruleCondition) return;
    } else {
        ruleName = prompt('Rule name:');
        if (!ruleName) return;
        ruleCondition = prompt('Condition:');
        if (!ruleCondition) return;
    }

    try {
        var res = await apiCall('/api/v1/training/automation/rules', {
            method: 'POST',
            body: JSON.stringify({
                id: 'rule-' + Date.now(),
                name: ruleName,
                condition: ruleCondition,
                enabled: true
            })
        });
        if (res && (res.status === 'created' || res.status === 'updated')) {
            if (window.VToast) VToast.success('Rule added: ' + ruleName);
            loadTrainingRules();
        }
    } catch (err) {
        if (window.VToast) VToast.error('Failed to create rule');
    }
}

async function loadTrainingRules() {
    var list = document.getElementById('trainingRulesList');
    if (!list) return;

    try {
        var data = await apiCall('/api/v1/training/automation/rules');
        if (data && data.rules && data.rules.length > 0) {
            list.innerHTML = data.rules.map(function(rule) {
                return '<div class="rule-card">' +
                    '<div class="rule-card__header">' +
                    '<strong>' + escapeHtml(rule.name || 'Unnamed Rule') + '</strong>' +
                    '<span class="badge ' + (rule.enabled ? 'badge--success' : 'badge--muted') + '">' +
                    (rule.enabled ? 'Active' : 'Disabled') + '</span>' +
                    '</div>' +
                    '<p class="text-secondary">' + escapeHtml(rule.condition || '—') + '</p>' +
                    '</div>';
            }).join('');
        } else {
            list.innerHTML = '<div class="empty-state"><i class="fas fa-cogs"></i>' +
                '<p>No automation rules configured</p>' +
                '<p class="empty-state-hint">Add rules to trigger training automatically based on conditions.</p></div>';
        }
    } catch (err) {
        // Keep existing content on error
    }
}

async function evolveNow() {
    if (window.VToast) VToast.info('Starting prompt evolution cycle...');
    try {
        // Use the training start endpoint with an evolution flag
        var res = await apiCall('/api/v1/training/start', {
            method: 'POST',
            body: JSON.stringify(buildTrainingStartPayload({ mode: 'evolve' }))
        });
        if (res && !res.error) {
            if (window.VToast) VToast.success('Prompt evolution cycle started');
        } else {
            if (window.VToast) VToast.warning('Evolution not available: ' + (res.error || 'Unknown'));
        }
    } catch (err) {
        if (window.VToast) VToast.error('Evolution cycle failed');
    }
}

async function pauseAllTraining() {
    var confirmed = true;
    if (window.VModal && VModal.confirm) {
        confirmed = await VModal.confirm('Pause all training operations? This includes idle-time and scheduled training.');
    }
    if (!confirmed) return;
    try {
        var res = await apiCall('/api/v1/training/pause', { method: 'POST' });
        if (res && !res.error) {
            if (window.VToast) VToast.warning('All training paused');
            var toggle = document.getElementById('idleTrainingToggle');
            if (toggle) toggle.checked = false;
        }
    } catch (err) {
        if (window.VToast) VToast.error('Failed to pause training');
    }
}

async function toggleIdleTraining(enabled) {
    try {
        if (enabled) {
            var res = await apiCall('/api/v1/training/resume', { method: 'POST' });
            if (res && !res.error) {
                if (window.VToast) VToast.success('Idle-time training enabled');
            }
        } else {
            var res2 = await apiCall('/api/v1/training/pause', { method: 'POST' });
            if (res2 && !res2.error) {
                if (window.VToast) VToast.info('Idle-time training disabled');
            }
        }
    } catch (err) {
        if (window.VToast) VToast.error('Failed to toggle idle training');
    }
}

// ── Phase 4: Settings — Custom Instructions ────────────────────────────────

async function saveCustomInstructions() {
    var textarea = document.getElementById('customInstructionsInput');
    if (!textarea) return;

    try {
        var res = await apiCall('/api/v1/preferences', {
            method: 'PUT',
            body: JSON.stringify({ customInstructions: textarea.value })
        });
        if (res && !res.error) {
            if (window.VToast) VToast.success('Custom instructions saved');
        } else {
            if (window.VToast) VToast.error('Failed to save: ' + (res.error || 'Unknown error'));
        }
    } catch (err) {
        if (window.VToast) VToast.error('Failed to save custom instructions');
    }
}

// Tasks - Shows all projects and their tasks
async function loadTasks() {
    let list = document.getElementById('queueTasksList');
    if (!list) {
        // Try alternate selectors
        list = document.querySelector('#tasksView #tasksList');
    }
    if (!list) return;
    list.innerHTML = '<div class="loading"><div class="spinner"></div></div>';

    try {
        const res = await fetch('/api/projects');
        const data = await safeJsonParse(res);

        if (data.error) {
            list.innerHTML = '<p>Error loading projects: ' + data.error + '</p>';
            return;
        }

        const projects = data.projects || [];

        if (projects.length === 0) {
            list.innerHTML = `
                <div class="empty-state">
                    <i class="fas fa-tasks"></i>
                    <p>No tasks yet. Create a project first.</p>
                    <button class="btn btn-small btn-primary" data-action="switchView" data-id="prompt">
                        <i class="fas fa-plus"></i> New Project
                    </button>
                </div>`;
            return;
        }

        list.innerHTML = projects.map(project => {
            const tasks = project.tasks || [];
            const statusClass = project.status === 'completed' ? 'completed' :
                               project.status === 'running' ? 'running' : 'pending';

            return `
                <div class="project-section">
                    <div class="project-header" data-action="toggleProjectTasks" data-id="${project.id}">
                        <div class="project-status ${statusClass}"></div>
                        <div class="project-info">
                            <span class="project-name">${project.name}</span>
                            <span class="project-goal">${project.goal || project.description || 'No goal'}</span>
                        </div>
                        <div class="project-meta">
                            <span class="task-count">${tasks.length} tasks</span>
                            <span class="status-badge ${statusClass}">${project.status}</span>
                        </div>
                        <i class="fas fa-chevron-down toggle-icon" id="toggle-${project.id}"></i>
                    </div>
                    <div class="project-tasks" id="tasks-${project.id}">
                        ${tasks.length === 0 ? '<p class="no-tasks">No tasks</p>' : tasks.map(task => {
                            const taskStatusClass = task.status || 'pending';
                            return `
                                <div class="task-item ${taskStatusClass}">
                                    <div class="task-status ${taskStatusClass}">
                                        ${taskStatusClass === 'completed' ? '<i class="fas fa-check"></i>' :
                                          taskStatusClass === 'running' ? '<i class="fas fa-spinner fa-spin"></i>' :
                                          '<i class="fas fa-clock"></i>'}
                                    </div>
                                    <div class="task-info">
                                        <span class="task-name">${task.id}</span>
                                        <span class="task-desc">${task.description || 'No description'}</span>
                                        <span class="task-model"><i class="fas fa-microchip"></i> ${task.assigned_model || 'Not assigned'}</span>
                                    </div>
                                    <div class="task-actions">
                                        ${taskStatusClass !== 'completed' ? `
                                            <button class="btn btn-small btn-secondary" data-action="runProjectTask" data-id="${project.id}" data-arg2="${task.id}">
                                                <i class="fas fa-play"></i> Run
                                            </button>
                                        ` : ''}
                                        <button class="btn btn-small btn-secondary" data-action="viewProjectTaskOutput" data-id="${project.id}" data-arg2="${task.id}">
                                            <i class="fas fa-eye"></i> View
                                        </button>
                                    </div>
                                </div>
                            `;
                        }).join('')}
                    </div>
                </div>
            `;
        }).join('');

    } catch (error) {
        list.innerHTML = '<p>Error loading projects</p>';
    }
}

// Toggle project tasks visibility
function toggleProjectTasks(projectId) {
    const tasksEl = document.getElementById(`tasks-${projectId}`);
    const toggleEl = document.getElementById(`toggle-${projectId}`);
    if (tasksEl) {
        tasksEl.classList.toggle('expanded');
        if (toggleEl) {
            toggleEl.classList.toggle('expanded');
        }
    }
}

// Run a specific task in a project
async function runProjectTask(projectId, taskId) {
    addActivity(`Running task ${taskId} in project ${projectId}...`);
    // For now, just refresh to show status
    loadTasks();
}

// View task output from a project
async function viewProjectTaskOutput(projectId, taskId) {
    const outputEl = document.getElementById('codeOutput');
    if (!outputEl) {
        switchView('output');
        return;
    }

    outputEl.innerHTML = '<div class="loading"><div class="spinner"></div></div>';

    try {
        const res = await fetch(`/api/v1/project/${projectId}/task/${taskId}/output`);
        const data = await safeJsonParse(res);

        if (data.error) {
            outputEl.textContent = 'Error loading output: ' + data.error;
            return;
        }

        if (data.output) {
            var outputText = data.output;
            if (outputText.length > TRUNCATE_DISPLAY_LIMIT) {
                outputEl.textContent = outputText.substring(0, TRUNCATE_DISPLAY_LIMIT);
                outputEl.innerHTML += buildTruncationNotice(outputText.length, projectId, taskId);
            } else {
                outputEl.textContent = outputText;
            }

            // Show generated files if any
            if (data.files && data.files.length > 0) {
                outputEl.innerHTML += '\n\n--- Generated Files ---\n';
                data.files.forEach(f => {
                    outputEl.innerHTML += `\n${escapeHtml(f.name)}`;
                });
            }
        } else {
            outputEl.textContent = 'No output available for task ' + taskId;
        }

        // Switch to output view
        switchView('output');
    } catch (error) {
        outputEl.textContent = 'Error loading output';
    }
}

// Make functions global
window.toggleProjectTasks = toggleProjectTasks;
window.runProjectTask = runProjectTask;
window.viewProjectTaskOutput = viewProjectTaskOutput;

async function runTask(taskId) {
    if (!currentProjectId) {
        addActivity('No project selected — create a project first', 'error');
        return;
    }
    addActivity(`Running task ${taskId}...`);
    try {
        const res = await fetch(`/api/project/${currentProjectId}/execute`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
            body: JSON.stringify({ task_id: taskId })
        });
        const data = await safeJsonParse(res);
        if (data.status === 'started' || data.status === 'running') {
            addActivity(`Task ${taskId} started`);
            setTimeout(loadTasks, 5000);
        } else {
            addActivity(`Error: ${data.error || 'Unknown error'}`, 'error');
        }
    } catch (error) {
        addActivity(`Error running task: ${error}`, 'error');
    }
}

// Output — loads task list from project workflow
async function loadOutput() {
    const select = document.getElementById('outputTaskSelect');
    if (!select) return;

    try {
        const res = await fetch('/api/v1/workflow');
        const data = await safeJsonParse(res);

        const projects = data.projects || [];
        const allTasks = [];
        projects.forEach(p => {
            (p.tasks || []).forEach(t => {
                if (!t.id) return;  // skip tasks without IDs (not yet executed)
                allTasks.push({ project_id: p.id, project_name: p.name, task_id: t.id, description: t.description || t.id, status: t.status });
            });
        });

        if (allTasks.length === 0) {
            select.innerHTML = '<option value="">No tasks yet — create a project first</option>';
            const outputEl = document.getElementById('codeOutput');
            if (outputEl) outputEl.textContent = 'No task output yet. Create a project and run some tasks.';
            return;
        }

        let currentSelection = select.value;
        select.innerHTML = '<option value="">Select a task...</option>';

        let currentProject = '';
        allTasks.forEach(t => {
            if (t.project_id !== currentProject) {
                const optgroup = document.createElement('optgroup');
                optgroup.label = t.project_name || t.project_id;
                select.appendChild(optgroup);
                currentProject = t.project_id;
            }
            const option = document.createElement('option');
            option.value = JSON.stringify({project_id: t.project_id, task_id: t.task_id});
            option.textContent = `${t.task_id}: ${(t.description || '').substring(0, 50)}`;
            option.dataset.projectId = t.project_id;
            option.dataset.taskId = t.task_id;
            select.appendChild(option);
        });

        if (currentSelection) {
            const found = Array.from(select.options).some(o => o.value === currentSelection);
            if (found) select.value = currentSelection;
        }
        if (!select.value && allTasks.length > 0) {
            const firstTask = allTasks.find(t => t.project_id === currentProjectId) || allTasks[0];
            if (firstTask) select.value = JSON.stringify({project_id: firstTask.project_id, task_id: firstTask.task_id});
        }
        if (select.value) loadOutputForTask();
    } catch (error) {
        console.error('Error loading output:', error);
    }
}

async function loadOutputForTask() {
    const select = document.getElementById('outputTaskSelect');
    const outputEl = document.getElementById('codeOutput');
    if (!select || !outputEl) return;

    const selectedValue = select.value;
    if (!selectedValue) {
        outputEl.textContent = 'Select a task to view its output';
        return;
    }

    try {
        let taskData;
        try {
            taskData = JSON.parse(selectedValue);
        } catch {
            // Fallback for old format
            taskData = {project_id: null, task_id: selectedValue};
        }

        if (!taskData.project_id) {
            outputEl.textContent = 'No project context — select a project-scoped task';
            return;
        }
        if (!taskData.task_id) {
            outputEl.textContent = 'Task has no ID — it may not have been executed yet';
            return;
        }
        const url = `/api/project/${taskData.project_id}/task/${taskData.task_id}/output`;

        outputEl.innerHTML = '<div class="loading"><div class="spinner"></div></div>';

        const res = await fetch(url);
        const data = await safeJsonParse(res);

        if (data.error) {
            outputEl.textContent = 'Error loading output: ' + data.error;
            return;
        }

        if (data.output) {
            var outputText = data.output;
            if (outputText.length > TRUNCATE_DISPLAY_LIMIT) {
                outputEl.textContent = outputText.substring(0, TRUNCATE_DISPLAY_LIMIT);
                outputEl.innerHTML += buildTruncationNotice(outputText.length, taskData.project_id, taskData.task_id);
            } else {
                outputEl.textContent = outputText;
            }

            // Show generated files if any
            if (data.files && data.files.length > 0) {
                outputEl.innerHTML += '\n\n--- Generated Files ---\n';
                data.files.forEach(f => {
                    outputEl.innerHTML += `\n${escapeHtml(f.name)}`;
                });
            }
        } else {
            outputEl.textContent = 'No output available for this task';
        }

    } catch (error) {
        outputEl.textContent = 'Error loading output: ' + error;
    }
}

async function viewTaskOutput(taskId, projectId) {
    const outputEl = document.getElementById('codeOutput');
    if (!outputEl) return;

    const pid = projectId || currentProjectId;
    if (!pid) {
        outputEl.textContent = 'No project selected — cannot load task output';
        return;
    }

    outputEl.innerHTML = '<div class="loading"><div class="spinner"></div></div>';

    try {
        const res = await fetch(`/api/v1/project/${pid}/task/${taskId}/output`);
        const data = await safeJsonParse(res);

        if (data.error) {
            outputEl.textContent = 'Error loading output: ' + data.error;
            return;
        }

        var fullOutput = data.output || '';
        if (fullOutput.length > TRUNCATE_DISPLAY_LIMIT) {
            outputEl.textContent = fullOutput.substring(0, TRUNCATE_DISPLAY_LIMIT);
            outputEl.innerHTML += buildTruncationNotice(fullOutput.length, pid, taskId);
        } else {
            outputEl.textContent = fullOutput || 'No output available';
        }
    } catch (error) {
        outputEl.textContent = 'Error loading output';
    }
}

/**
 * Build an HTML truncation notice banner for output that exceeds the display limit.
 *
 * @param {number} fullLength - Total character count of the original output.
 * @param {string} projectId  - Project identifier for the API link.
 * @param {string} taskId     - Task identifier for the API link.
 * @returns {string} HTML string for the notice element.
 */
function buildTruncationNotice(fullLength, projectId, taskId) {
    var sizeKB = VFmt.decimal(fullLength / 1024, 1);
    return '<div class="truncation-notice">' +
        '<i class="fas fa-exclamation-triangle"></i> ' +
        'Output truncated (' + sizeKB + ' KB). ' +
        '<a href="#" onclick="viewFullOutput(\'' + projectId + '\', \'' + taskId + '\'); return false;">View full output</a>' +
        '</div>';
}

/**
 * Fetch the full (un-truncated) task output and display it in CodeCanvas or the output panel.
 *
 * @param {string} projectId - Project identifier.
 * @param {string} taskId    - Task identifier.
 */
async function viewFullOutput(projectId, taskId) {
    var outputEl = document.getElementById('codeOutput');
    if (!outputEl) return;

    outputEl.innerHTML = '<div class="loading"><div class="spinner"></div></div>';

    try {
        var res = await fetch('/api/project/' + projectId + '/task/' + taskId + '/output');
        var data = await safeJsonParse(res);

        if (data.error) {
            outputEl.textContent = 'Error loading full output: ' + data.error;
            return;
        }

        if (data.output && window.CodeCanvas) {
            CodeCanvas.open([{
                filename: taskId + '-output.txt',
                language: 'text',
                content: data.output
            }]);
        } else {
            outputEl.textContent = data.output || 'No output available';
        }
    } catch (error) {
        outputEl.textContent = 'Error loading full output';
    }
}
window.viewFullOutput = viewFullOutput;

// Settings
async function loadSettings() {
    const modelsDirInput = document.getElementById('modelsDirInput');
    const configInput = document.getElementById('configInput');
    const gpuLayersInput = document.getElementById('gpuLayersInput');

    try {
        const res = await fetch('/api/v1/status');
        const data = await safeJsonParse(res);

        if (data.error) {
            console.error('Error loading settings:', data.error);
            return;
        }

        if (modelsDirInput) modelsDirInput.value = data.models_dir || '';
        if (configInput) configInput.value = data.config_path || '';
        if (gpuLayersInput) gpuLayersInput.value = data.gpu_layers ?? -1;

    } catch (error) {
        console.error('Error loading settings:', error);
    }
}

// Agent Status Panel
async function refreshAgentStatus() {
    const grid = document.getElementById('agentStatusGrid');
    if (!grid) return;

    grid.innerHTML = '<div class="loading"><div class="spinner"></div></div>';

    try {
        const res = await fetch('/api/v1/agents/status');
        const data = await safeJsonParse(res);

        if (data.error) {
            grid.innerHTML = '<p style="color: var(--error);">Error: ' + data.error + '</p>';
            return;
        }

        const agents = data.agents || [];

        if (agents.length === 0) {
            grid.innerHTML = '<p style="color: var(--text-secondary);">No agents initialized. Go to Agents view to initialize.</p>';
            return;
        }

        const agentIcons = {
            explorer: 'fa-compass',
            librarian: 'fa-book',
            oracle: 'fa-globe',
            'ui-planner': 'fa-palette',
            builder: 'fa-hammer',
            researcher: 'fa-search',
            evaluator: 'fa-check-circle',
            synthesizer: 'fa-brain'
        };

        grid.innerHTML = agents.map(agent => `
            <div class="agent-status-card">
                <div class="agent-status-icon ${escapeHtml(agent.type)}">
                    <i class="fas ${agentIcons[agent.type] || 'fa-robot'}"></i>
                </div>
                <div class="agent-status-info">
                    <div class="agent-status-name">${escapeHtml(agent.name)}</div>
                    <div class="agent-status-state">${escapeHtml(agent.state || 'idle')}</div>
                </div>
                <div class="agent-status-dot ${agent.state === 'active' ? 'active' : 'idle'}"></div>
            </div>
        `).join('');

    } catch (error) {
        console.error('Error loading agent status:', error);
        grid.innerHTML = '<p style="color: var(--error);">Error loading agents</p>';
    }
}

// Shared Memory Panel
async function refreshSharedMemory() {
    const list = document.getElementById('sharedMemoryList');
    if (!list) return;

    try {
        const res = await fetch('/api/v1/memory');
        const data = await safeJsonParse(res);

        if (data.error) {
            list.innerHTML = '<p style="color: var(--error);">Error: ' + data.error + '</p>';
            return;
        }

        const memories = data.items || data.memories || [];

        if (memories.length === 0) {
            list.innerHTML = '<div class="empty-state"><i class="fas fa-memory"></i><p>No memories yet</p></div>';
            return;
        }

        const memoryIcons = {
            intent: 'fa-bullseye',
            discovery: 'fa-lightbulb',
            decision: 'fa-balance-scale',
            problem: 'fa-exclamation-triangle',
            solution: 'fa-check',
            pattern: 'fa-repeat',
            warning: 'fa-shield-alt'
        };

        list.innerHTML = memories.slice(0, 10).map(mem => `
            <div class="memory-item">
                <i class="fas ${memoryIcons[mem.type] || 'fa-memory'} memory-icon"></i>
                <div class="memory-content">
                    <div>${mem.content?.substring(0, 80) || ''}...</div>
                    <div class="memory-meta">
                        <span class="memory-tag">${mem.type}</span>
                        <span>${mem.agent}</span>
                    </div>
                </div>
            </div>
        `).join('');

    } catch (error) {
        console.error('Error loading shared memory:', error);
        list.innerHTML = '<p style="color: var(--error);">Error loading memory</p>';
    }
}

// Initialize Agents
async function initializeAgents() {
    addActivity('Initializing multi-agent orchestrator...');

    try {
        const res = await fetch('/api/v1/agents/initialize', { method: 'POST', headers: {'X-Requested-With': 'XMLHttpRequest'} });
        const data = await safeJsonParse(res);

        if (data.error) {
            addActivity('Error initializing agents: ' + data.error, 'error');
            return;
        }

        addActivity('Agents initialized: ' + (data.agents?.join(', ') || 'None'));
        refreshAgentStatus();
        loadAgentsView();

    } catch (error) {
        console.error('Error initializing agents:', error);
        addActivity('Error initializing agents', 'error');
    }
}

// Load Agents View
async function loadAgentsView() {
    await refreshAgentStatus();
    await refreshSharedMemory();
    await loadActiveAgents();
    await loadAgentTasks();
    await loadDecisionPanel();
}

async function loadActiveAgents() {
    const list = document.getElementById('activeAgentsList');
    if (!list) return;

    try {
        const res = await fetch('/api/v1/agents/active');
        const data = await safeJsonParse(res);

        if (data.error) {
            list.innerHTML = '<p style="color: var(--error);">Error: ' + data.error + '</p>';
            return;
        }

        const agents = data.agents || [];

        if (agents.length === 0) {
            list.innerHTML = '<div class="empty-state"><i class="fas fa-robot"></i><p>No active agents</p></div>';
            return;
        }

        list.innerHTML = agents.map(agent => `
            <div class="active-agent-item">
                <div class="active-agent-avatar" style="background: ${escapeHtml(agent.color || 'var(--accent)')}">
                    <i class="fas ${escapeHtml(agent.icon || 'fa-robot')}"></i>
                </div>
                <div class="active-agent-info">
                    <div class="active-agent-name">${escapeHtml(agent.name)}</div>
                    <div class="active-agent-role">${escapeHtml(agent.role)}</div>
                </div>
                <div class="active-agent-tasks">${agent.tasks_completed || 0} tasks</div>
            </div>
        `).join('');

    } catch (error) {
        console.error('Error loading active agents:', error);
    }
}

async function loadAgentTasks() {
    const list = document.getElementById('agentTasksList');
    if (!list) return;

    try {
        const res = await fetch('/api/v1/agents/tasks');
        const data = await safeJsonParse(res);

        if (data.error) {
            list.innerHTML = '<p style="color: var(--error);">Error: ' + data.error + '</p>';
            return;
        }

        const tasks = data.tasks || [];

        if (tasks.length === 0) {
            list.innerHTML = '<div class="empty-state"><i class="fas fa-tasks"></i><p>No agent tasks</p></div>';
            return;
        }

        list.innerHTML = tasks.map(task => `
            <div class="agent-task-item">
                <span class="agent-task-status ${escapeHtml(task.status)}"></span>
                <div class="agent-task-info">${escapeHtml(task.description)}</div>
                <span class="agent-task-agent">${escapeHtml(task.agent)}</span>
            </div>
        `).join('');

    } catch (error) {
        console.error('Error loading agent tasks:', error);
    }
}

function formatDecisionPrompt(prompt) {
    // Detect if the prompt is a JSON auto-adjustment record from the AutoTuner
    if (typeof prompt === 'string' && prompt.trimStart().startsWith('{')) {
        try {
            const obj = JSON.parse(prompt);
            if ('parameter' in obj && 'old' in obj && 'new' in obj) {
                const autoLabel = obj.auto
                    ? '<span style="color:var(--success);font-weight:600;">Auto-applied</span>'
                    : '<span style="color:var(--warning);font-weight:600;">Manual action needed</span>';
                return `<div style="display:flex;flex-direction:column;gap:0.4rem;">
                    <div><strong>Parameter:</strong> <code>${escapeHtml(String(obj.parameter))}</code></div>
                    <div><strong>Change:</strong> <code>${escapeHtml(String(obj.old))}</code> &rarr; <code>${escapeHtml(String(obj.new))}</code></div>
                    ${obj.rationale ? `<div><strong>Rationale:</strong> ${escapeHtml(String(obj.rationale))}</div>` : ''}
                    <div>${autoLabel}</div>
                </div>`;
            }
        } catch (_) {
            // Not valid JSON — fall through to plain text rendering
        }
    }
    return escapeHtml(String(prompt));
}

async function loadDecisionPanel() {
    const panel = document.getElementById('decisionPanel');
    if (!panel) return;

    try {
        const res = await fetch('/api/v1/decisions/pending');
        const data = await safeJsonParse(res);

        if (data.error) {
            panel.innerHTML = '<p style="color: var(--error);">Error: ' + data.error + '</p>';
            return;
        }

        const decisions = data.decisions || [];

        if (decisions.length === 0) {
            panel.innerHTML = '<p style="color: var(--text-secondary); font-size: 0.85rem;">No pending decisions</p>';
            return;
        }

        panel.innerHTML = decisions.map((dec, i) => {
            const promptHtml = formatDecisionPrompt(dec.prompt);
            // Check if this is an auto-applied decision (no user action needed)
            let isAutoApplied = false;
            try {
                const parsed = typeof dec.prompt === 'string' && dec.prompt.trimStart().startsWith('{') ? JSON.parse(dec.prompt) : null;
                if (parsed && parsed.auto) isAutoApplied = true;
            } catch (_) { /* not JSON */ }
            const optionsHtml = (dec.options || []).map((opt, j) => `
                <div class="decision-option" data-action="selectDecisionOption" data-id="${i}" data-arg2="${j}">
                    <div class="decision-option-radio"></div>
                    <div class="decision-option-text">
                        <div class="decision-option-label">${escapeHtml(opt.label || String(opt))}</div>
                        ${opt.pros ? `<div class="decision-option-pros">+ ${escapeHtml(opt.pros)}</div>` : ''}
                        ${opt.cons ? `<div class="decision-option-cons">- ${escapeHtml(opt.cons)}</div>` : ''}
                    </div>
                </div>
            `).join('');
            const submitBtn = isAutoApplied
                ? ''
                : `<button class="btn btn-primary btn-small decision-submit" data-action="submitDecision" data-id="${i}">Submit Decision</button>`;
            return `
            <div class="decision-item">
                <div class="decision-prompt">${promptHtml}</div>
                ${optionsHtml ? `<div class="decision-options">${optionsHtml}</div>` : ''}
                ${submitBtn}
            </div>`;
        }).join('');

        window.pendingDecisions = decisions;

    } catch (error) {
        console.error('Error loading decision panel:', error);
    }
}

function selectDecisionOption(decisionIndex, optionIndex) {
    const options = document.querySelectorAll(`.decision-item:nth-child(${decisionIndex + 1}) .decision-option`);
    options.forEach((opt, i) => {
        opt.classList.toggle('selected', i === optionIndex);
    });
    window.selectedDecision = { decisionIndex, optionIndex };
}

async function submitDecision(decisionIndex) {
    const selection = window.selectedDecision;
    if (!selection || selection.decisionIndex !== decisionIndex) {
        alert('Please select an option');
        return;
    }

    const decision = window.pendingDecisions[decisionIndex];
    const option = decision.options[selection.optionIndex];

    try {
        const res = await fetch('/api/v1/decisions', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
            body: JSON.stringify({
                decision_id: decision.id,
                choice: option.label
            })
        });
        const data = await safeJsonParse(res);

        if (data.error) {
            alert('Error: ' + data.error);
            return;
        }

        addActivity('Decision made: ' + option.label);
        loadDecisionPanel();

    } catch (error) {
        console.error('Error submitting decision:', error);
    }
}

window.refreshAgentStatus = refreshAgentStatus;
window.refreshSharedMemory = refreshSharedMemory;
window.initializeAgents = initializeAgents;
window.selectDecisionOption = selectDecisionOption;
window.submitDecision = submitDecision;

async function loadCredentials() {
    const listEl = document.getElementById('credentialsList');
    if (!listEl) return;

    try {
        const res = await fetch('/api/admin/credentials');
        const data = await safeJsonParse(res);

        if (data.error) {
            listEl.innerHTML = '<p style="color: var(--error);">Error loading credentials</p>';
            return;
        }

        const health = data.health || {};
        const credentials = data.credentials || {};

        if (Object.keys(health).length === 0) {
            listEl.innerHTML = '<p style="color: var(--text-secondary);">No credentials configured. Add tokens below to enable live model search.</p>';
            return;
        }

        listEl.innerHTML = Object.entries(health).map(([source, info]) => `
            <div class="credential-item" style="display: flex; justify-content: space-between; align-items: center; padding: 0.5rem; background: var(--bg-card); border-radius: var(--radius); margin-bottom: 0.5rem;">
                <div>
                    <strong>${escapeHtml(source)}</strong>
                    <span style="color: ${info.enabled ? 'var(--success)' : 'var(--error)'}; font-size: 0.8rem;">
                        ${info.enabled ? 'Enabled' : 'Disabled'}
                    </span>
                    <div style="font-size: 0.75rem; color: var(--text-secondary);">
                        Last rotated: ${info.last_rotated ? escapeHtml(info.last_rotated.split('T')[0]) : 'Never'}
                        ${info.needs_rotation ? '<span style="color: var(--warning);"> - Rotation needed!</span>' : ''}
                    </div>
                </div>
                <button class="btn btn-small btn-secondary" data-action="deleteCredential" data-id="${escapeHtml(source)}" title="Delete">
                    <i class="fas fa-trash"></i>
                </button>
            </div>
        `).join('');

    } catch (error) {
        console.error('Error loading credentials:', error);
        listEl.innerHTML = '<p style="color: var(--error);">Error loading credentials</p>';
    }
}

document.getElementById('saveCredentialBtn')?.addEventListener('click', async () => {
    const source = document.getElementById('credentialSourceSelect')?.value;
    const token = document.getElementById('credentialTokenInput')?.value;
    const rotation = document.getElementById('credentialRotationInput')?.value;

    if (!token) {
        alert('Please enter a token');
        return;
    }

    try {
        const res = await fetch(`/api/admin/credentials/${source}`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest'},
            body: JSON.stringify({
                token: token,
                credential_type: 'bearer',
                rotation_days: parseInt(rotation) || 30
            })
        });

        const data = await safeJsonParse(res);

        if (data.error) {
            alert('Error: ' + data.error);
            return;
        }

        showStatusBanner('Credential saved successfully', 'success');
        document.getElementById('credentialTokenInput').value = '';
        loadCredentials();

    } catch (error) {
        alert('Error saving credential');
    }
});

async function deleteCredential(source) {
    if (!confirm(`Delete credential for ${source}?`)) return;

    try {
        const res = await fetch(`/api/admin/credentials/${source}`, {
            method: 'DELETE',
            headers: {'X-Requested-With': 'XMLHttpRequest'}
        });

        const data = await safeJsonParse(res);

        if (data.error) {
            alert('Error: ' + data.error);
            return;
        }

        showStatusBanner('Credential deleted', 'success');
        loadCredentials();

    } catch (error) {
        alert('Error deleting credential');
    }
}

document.getElementById('saveConfig')?.addEventListener('click', async () => {
    const modelsDir = document.getElementById('modelsDirInput')?.value;
    const config = document.getElementById('configInput')?.value;
    const gpuLayers = document.getElementById('gpuLayersInput')?.value;
    const btn = document.getElementById('saveConfig');

    // Show saving state
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Saving...';
    }

    try {
        const res = await fetch('/api/v1/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
            body: JSON.stringify({ models_dir: modelsDir, config_path: config, gpu_layers: gpuLayers })
        });
        const data = await safeJsonParse(res);

        if (data.status === 'updated') {
            addActivity('Settings saved successfully!');
            // Show success briefly then reload page
            setTimeout(() => {
                window.location.reload();
            }, 1000);
        } else {
            addActivity('Error saving settings: ' + (data.error || 'Unknown error'), 'error');
        }
    } catch (error) {
        addActivity('Error saving settings: ' + error, 'error');
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = '<i class="fas fa-save"></i> Save Settings';
        }
    }
});

// Model Configuration
async function loadModelConfig() {
    try {
        const res = await fetch('/api/v1/model-config');
        const data = await safeJsonParse(res);

        if (data.error) {
            console.error('Error loading model config:', data.error);
            return;
        }

        const memoryInput = document.getElementById('memoryBudgetInput');
        const defaultModelsInput = document.getElementById('defaultModelsInput');
        const fallbackModelsInput = document.getElementById('fallbackModelsInput');
        const uncensoredModelsInput = document.getElementById('uncensoredModelsInput');

        if (memoryInput) memoryInput.value = data.memory_budget_gb || 32;
        if (defaultModelsInput) defaultModelsInput.value = (data.default_models || []).join(', ');
        if (fallbackModelsInput) fallbackModelsInput.value = (data.fallback_models || []).join(', ');
        if (uncensoredModelsInput) uncensoredModelsInput.value = (data.uncensored_fallback_models || []).join(', ');

        // Load status and models in parallel (models uses TTL cache, so fast)
        const [statusRes, modelsRes] = await Promise.all([
            fetch('/api/v1/status'),
            fetch('/api/v1/models')
        ]);
        const [statusData, modelsData] = await Promise.all([
            safeJsonParse(statusRes),
            safeJsonParse(modelsRes)
        ]);

        const activeModelDisplay = document.getElementById('activeModelDisplay');
        if (activeModelDisplay) {
            activeModelDisplay.innerHTML = statusData.active_model_id
                ? `<strong>${statusData.active_model_id}</strong>`
                : '<span style="color: var(--text-secondary);">No model active</span>';
        }

        const modelSwapSelect = document.getElementById('modelSwapSelect');
        if (modelSwapSelect && modelsData.models && modelsData.models.length > 0) {
            modelSwapSelect.innerHTML = '<option value="">Select a model...</option>' +
                modelsData.models.map(m => `<option value="${m.id}">${m.name || m.id}</option>`).join('');
        } else if (modelSwapSelect) {
            modelSwapSelect.innerHTML = '<option value="">No models — click Discover</option>';
        }

    } catch (error) {
        console.error('Error loading model config:', error);
    }
}

document.getElementById('saveModelConfig')?.addEventListener('click', async () => {
    const memoryBudget = document.getElementById('memoryBudgetInput')?.value;
    const defaultModels = document.getElementById('defaultModelsInput')?.value;
    const fallbackModels = document.getElementById('fallbackModelsInput')?.value;
    const uncensoredModels = document.getElementById('uncensoredModelsInput')?.value;
    const btn = document.getElementById('saveModelConfig');

    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Saving...';
    }

    try {
        const res = await fetch('/api/v1/model-config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
            body: JSON.stringify({
                memory_budget_gb: parseInt(memoryBudget) || 32,
                default_models: defaultModels ? defaultModels.split(',').map(s => s.trim()).filter(s => s) : [],
                fallback_models: fallbackModels ? fallbackModels.split(',').map(s => s.trim()).filter(s => s) : [],
                uncensored_fallback_models: uncensoredModels ? uncensoredModels.split(',').map(s => s.trim()).filter(s => s) : []
            })
        });
        const data = await safeJsonParse(res);

        if (data.status === 'updated') {
            addActivity('Model configuration saved!');
        } else {
            addActivity('Error saving model config: ' + (data.error || 'Unknown error'), 'error');
        }
    } catch (error) {
        addActivity('Error saving model config: ' + error, 'error');
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = '<i class="fas fa-save"></i> Save Model Config';
        }
    }
});

document.getElementById('swapModelBtn')?.addEventListener('click', async () => {
    const modelSelect = document.getElementById('modelSwapSelect');
    const newModel = modelSelect?.value;

    if (!newModel) {
        addActivity('Please select a model to swap to', 'warning');
        return;
    }

    try {
        const res = await fetch('/api/v1/swap-model', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
            body: JSON.stringify({ model_id: newModel })
        });
        const data = await safeJsonParse(res);

        if (data.status === 'swapped') {
            addActivity(`Swapped to model: ${newModel}`);
            loadModelConfig();
        } else {
            addActivity('Error swapping model: ' + (data.error || 'Unknown error'), 'error');
        }
    } catch (error) {
        addActivity('Error swapping model: ' + error, 'error');
    }
});

// Archive View
async function loadArchive() {
    const list = document.getElementById('archiveList');
    if (!list) return;

    list.innerHTML = '<div class="loading"><div class="spinner"></div></div>';

    try {
        const searchQuery = document.getElementById('archiveSearch')?.value || '';
        const statusFilter = document.getElementById('archiveStatusFilter')?.value || '';

        let url = '/api/v1/workflow?include_archived=true';
        if (searchQuery) url += `&search=${encodeURIComponent(searchQuery)}`;
        if (statusFilter) url += `&status=${encodeURIComponent(statusFilter)}`;

        const res = await fetch(url);
        const data = await safeJsonParse(res);

        if (data.error) {
            list.innerHTML = '<p>Error loading archive: ' + data.error + '</p>';
            return;
        }

        const projects = (data.projects || []).filter(p => p.archived);

        if (projects.length === 0) {
            list.innerHTML = '<div class="empty-state"><i class="fas fa-archive"></i><p>No archived projects</p></div>';
            return;
        }

        list.innerHTML = projects.map(project => `
            <div class="archive-item">
                <div class="archive-item-header">
                    <span class="archive-item-name">${project.name}</span>
                    <div class="archive-item-actions">
                        <button class="btn btn-small btn-secondary" data-action="renameProject" data-id="${project.id}">
                            <i class="fas fa-edit"></i> Rename
                        </button>
                        <button class="btn btn-small btn-primary" data-action="unarchiveProject" data-id="${project.id}">
                            <i class="fas fa-box-open"></i> Unarchive
                        </button>
                        <button class="btn btn-small btn-danger" data-action="deleteProject" data-id="${project.id}">
                            <i class="fas fa-trash"></i>
                        </button>
                    </div>
                </div>
                <div class="archive-item-meta">
                    <span><i class="fas fa-tag"></i> ${project.status}</span>
                    <span><i class="fas fa-microchip"></i> ${project.model || 'N/A'}</span>
                    <span>${project.tasks?.length || 0} tasks</span>
                </div>
                ${project.goal ? `<p style="margin-top: 0.5rem; color: var(--text-secondary);">${project.goal}</p>` : ''}
            </div>
        `).join('');

    } catch (error) {
        console.error('Error loading archive:', error);
        list.innerHTML = '<p>Error loading archive</p>';
    }
}

async function renameProject(projectId) {
    const newName = prompt('Enter new project name:');
    if (!newName) return;

    const newDescription = prompt('Enter new description (or leave empty):') || '';

    try {
        const res = await fetch(`/api/project/${projectId}/rename`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
            body: JSON.stringify({ name: newName, description: newDescription })
        });
        const data = await safeJsonParse(res);

        if (data.status === 'renamed') {
            addActivity(`Project renamed to: ${newName}`);
            loadArchive();
            loadSidebarProjects();
        } else {
            addActivity('Error renaming project: ' + (data.error || 'Unknown error'), 'error');
        }
    } catch (error) {
        addActivity('Error renaming project: ' + error, 'error');
    }
}

async function unarchiveProject(projectId) {
    if (!confirm('Unarchive this project?')) return;

    try {
        const res = await fetch(`/api/project/${projectId}/archive`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
            body: JSON.stringify({ archive: false })
        });
        const data = await safeJsonParse(res);

        if (data.status === 'unarchived') {
            addActivity('Project unarchived');
            loadArchive();
            loadSidebarProjects();
            loadWorkflow();
        } else {
            addActivity('Error unarchiving project: ' + (data.error || 'Unknown error'), 'error');
        }
    } catch (error) {
        addActivity('Error unarchiving project: ' + error, 'error');
    }
}

async function archiveProject(projectId) {
    if (!confirm('Archive this project?')) return;

    try {
        const res = await fetch(`/api/project/${projectId}/archive`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
            body: JSON.stringify({ archive: true })
        });
        const data = await safeJsonParse(res);

        if (data.status === 'archived') {
            addActivity('Project archived');
            loadSidebarProjects();
            loadWorkflow();
        } else {
            addActivity('Error archiving project: ' + (data.error || 'Unknown error'), 'error');
        }
    } catch (error) {
        addActivity('Error archiving project: ' + error, 'error');
    }
}

async function deleteProject(projectId) {
    if (!confirm('Permanently delete this project? This cannot be undone.')) return;

    try {
        const res = await fetch(`/api/project/${projectId}`, {
            method: 'DELETE',
            headers: {'X-Requested-With': 'XMLHttpRequest'}
        });
        const data = await safeJsonParse(res);

        if (data.status === 'deleted' || !data.error) {
            addActivity('Project deleted');
            loadArchive();
            loadSidebarProjects();
        } else {
            addActivity('Error deleting project: ' + (data.error || 'Unknown error'), 'error');
        }
    } catch (error) {
        addActivity('Error deleting project: ' + error, 'error');
    }
}

/**
 * downloadProject: trigger a browser download of the project ZIP archive.
 */
function downloadProject(projectId) {
    const a = document.createElement('a');
    a.href = `/api/project/${projectId}/download`;
    a.download = `${projectId}.zip`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
}

// Make functions global for onclick handlers
window.runTask = runTask;
window.viewTaskOutput = viewTaskOutput;
window.renameProject = renameProject;
window.unarchiveProject = unarchiveProject;
window.archiveProject = archiveProject;
window.deleteProject = deleteProject;
window.downloadProject = downloadProject;
window.quickRename = quickRename;

// ==================== DELEGATED EVENT HANDLERS ====================
// Replaces all inline onclick/onmouseenter/onmouseleave handlers for CSP compliance.
// Elements use data-action="functionName", data-id="primaryArg", data-arg2="secondaryArg".
// data-stop-propagation="true" causes e.stopPropagation() before dispatch.

(function () {
    const actionHandlers = {
        searchResultClick: function (el) {
            const id = el.dataset.id;
            selectProjectFromSidebar(id);
            hideSearchResults();
        },
        switchView: function (el) {
            switchView(el.dataset.id);
        },
        selectProjectFromSidebar: function (el) {
            selectProjectFromSidebar(el.dataset.id);
        },
        quickRename: function (el) {
            quickRename(el.dataset.id);
        },
        archiveProject: function (el) {
            archiveProject(el.dataset.id);
        },
        showModelRanking: function (el) {
            showModelRanking(el.dataset.id, el.dataset.arg2);
        },
        toggleTaskExpand: function (el) {
            toggleTaskExpand(el.dataset.id);
        },
        selectOverrideModel: function (el) {
            selectOverrideModel(el.dataset.id, el.dataset.arg2);
        },
        toggleSubtasks: function (el) {
            toggleSubtasks(el.dataset.id);
        },
        submitFollowUp: function () {
            submitFollowUp();
        },
        toggleProjectTasks: function (el) {
            toggleProjectTasks(el.dataset.id);
        },
        loadModels: function () {
            loadModels();
        },
        discoverBtnProxy: function () {
            document.getElementById('discoverBtn').click();
        },
        downloadModel: function (el) {
            downloadModel(el.dataset.id, el.dataset.arg2);
        },
        runProjectTask: function (el) {
            runProjectTask(el.dataset.id, el.dataset.arg2);
        },
        viewProjectTaskOutput: function (el) {
            viewProjectTaskOutput(el.dataset.id, el.dataset.arg2);
        },
        selectDecisionOption: function (el) {
            selectDecisionOption(Number(el.dataset.id), Number(el.dataset.arg2));
        },
        submitDecision: function (el) {
            submitDecision(Number(el.dataset.id));
        },
        deleteCredential: function (el) {
            deleteCredential(el.dataset.id);
        },
        renameProject: function (el) {
            renameProject(el.dataset.id);
        },
        unarchiveProject: function (el) {
            unarchiveProject(el.dataset.id);
        },
        deleteProject: function (el) {
            deleteProject(el.dataset.id);
        },
        downloadProject: function (el) {
            downloadProject(el.dataset.id);
        },
    };

    function dispatchActionEvent(e) {
        const el = e.target.closest('[data-action]');
        if (!el) return false;

        const action = el.dataset.action;
        if (!actionHandlers[action] && typeof window[action] !== 'function') return false;

        if (el.dataset.stopPropagation === 'true') {
            e.stopPropagation();
        }

        // Prevent default for anchor tags to avoid navigation
        if (el.tagName === 'A') {
            e.preventDefault();
        }

        if (actionHandlers[action]) {
            actionHandlers[action](el);
        } else if (typeof window[action] === 'function') {
            // Fallback: call global function with data-id as argument
            window[action](el.dataset.id);
        }
        return true;
    }

    // Delegated click handler
    document.addEventListener('click', function (e) {
        dispatchActionEvent(e);
    });

    // Delegated keyboard activation for non-native data-action controls.
    document.addEventListener('keydown', function (e) {
        if (e.key !== 'Enter' && e.key !== ' ') return;
        const el = e.target.closest('[data-action][role="button"]');
        if (!el) return;
        e.preventDefault();
        dispatchActionEvent(e);
    });

    // Delegated mouseenter/mouseleave for elements with data-hover-action="rationale"
    document.addEventListener('mouseenter', function (e) {
        if (!e.target || !e.target.closest) return;
        const el = e.target.closest('[data-hover-action="rationale"]');
        if (el) showRationale(el);
    }, true);

    document.addEventListener('mouseleave', function (e) {
        if (!e.target || !e.target.closest) return;
        const el = e.target.closest('[data-hover-action="rationale"]');
        if (el) hideRationale(el);
    }, true);
})();

// ── Public API ──────────────────────────────────────────────────────────────
window.VApp = {
    // State
    get currentView() { return currentView; },
    set currentView(v) { currentView = v; },
    get currentProjectId() { return currentProjectId; },
    set currentProjectId(v) { currentProjectId = v; },
    get models() { return models; },
    get tasks() { return tasks; },

    // View state helpers
    showSkeleton,
    hideSkeleton,
    showErrorState,

    // Core functions
    switchView,
    apiCall,
    safeJsonParse,
    escapeHtml,
    showStatusBanner,
    hideStatusBanner,
    debounce,
    initTheme,
    toggleTheme,
    showShortcutHelp,
    applyInterfaceMode,

    // View loaders
    loadDashboard,
    loadModels,
    loadTasks,
    loadPrompt,
    loadSettings,
    loadTraining,
    loadWorkflow,
    loadOutput,
    loadArchive,
    loadAgentsView,
    loadDecomposition,
    loadMemory,
    searchMemory,
    deleteMemory,
    addMemoryEntry,
    memoryNextPage,
    memoryPrevPage,
    loadCurrentProject,
    loadProjectDetails,
    loadCredentials,
    loadModelConfig,

    // Project creation (used by intake-flow.js)
    createProject: createNewProject,
    sendChatMessage,

    // Actions (used by event-bindings.js and vetinari_extensions.js)
    refreshAgentStatus,
    refreshSharedMemory,
    expandAllTasks,
    collapseAllTasks,
    refreshModelSearch,
    applyModelOverride,
    cancelCurrentProject,
    initializeAgents,
    decomposeWithAgent,
    runAssignmentPass,
    exportTrainingData,
    startTraining,
    seedTrainingData,
    wireModelSearch,
    loadModelsRecommended,
    loadGpuStats,
    loadSystemResources,
    loadMemoryFiltered,

    // Phase 4 remediation: training actions
    quickStartTraining,
    quickCollectData,
    quickViewLastRun,
    saveTrainingConfig,
    dryRunTraining,
    pauseTraining,
    stopTraining,
    filterTrainingData,
    generateSyntheticData,
    addTrainingRule,
    loadTrainingRules,
    evolveNow,
    pauseAllTraining,
    toggleIdleTraining,
    saveCustomInstructions,
};

// Also expose switchView and subscribeToProjectStream on window directly so
// vetinari_extensions.js can capture and wrap them at parse time.
window.switchView = switchView;
window.subscribeToProjectStream = subscribeToProjectStream;
window.AccessibilityManager = AccessibilityManager;

// Global unhandled promise rejection handler
window.addEventListener('unhandledrejection', function(e) {
    console.warn('Unhandled promise rejection:', e.reason);
});

// Clean up SSE connections on page unload
window.addEventListener('beforeunload', function() {
    if (window.SSEManager && typeof SSEManager.closeAll === 'function') {
        SSEManager.closeAll();
    }
});

})();
