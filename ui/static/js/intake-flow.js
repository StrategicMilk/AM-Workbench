/**
 * IntakeFlow — Conversational project intake replacing the static 3-column form.
 *
 * Shows a grid of category cards (matching GoalCategory enum). Selecting one
 * collapses the grid into a badge and reveals category-specific quick-config
 * fields inline. The user can always skip the form and type directly into chat.
 *
 * Loaded AFTER app.js. Attaches to global window.IntakeFlow for event-bindings.
 */

'use strict';

(function () {

// ── Category definitions (mirrors vetinari/types.py GoalCategory) ────────

const CATEGORIES = [
    {
        id: 'code',
        label: 'Build Software',
        icon: 'fa-code',
        description: 'Web apps, APIs, CLI tools, libraries, scripts',
        accent: 'var(--primary)'
    },
    {
        id: 'research',
        label: 'Analyze & Research',
        icon: 'fa-microscope',
        description: 'Code analysis, data exploration, investigation',
        accent: 'var(--info)'
    },
    {
        id: 'docs',
        label: 'Write Documentation',
        icon: 'fa-file-alt',
        description: 'README, API docs, guides, technical writing',
        accent: 'var(--success)'
    },
    {
        id: 'creative',
        label: 'Create Content',
        icon: 'fa-palette',
        description: 'Creative writing, marketing copy, presentations',
        accent: 'var(--warning)'
    },
    {
        id: 'security',
        label: 'Security Audit',
        icon: 'fa-shield-alt',
        description: 'Vulnerability scanning, code review, threat analysis',
        accent: 'var(--danger)'
    },
    {
        id: 'data',
        label: 'Data & ML',
        icon: 'fa-chart-bar',
        description: 'Data pipelines, analysis, ML workflows',
        accent: 'var(--secondary)'
    },
    {
        id: 'devops',
        label: 'DevOps & Infra',
        icon: 'fa-server',
        description: 'CI/CD, deployment, containerization, monitoring',
        accent: 'var(--agent-operations)'
    },
    {
        id: 'ui',
        label: 'Design & UI',
        icon: 'fa-paint-brush',
        description: 'Frontend interfaces, design systems, prototypes',
        accent: 'var(--agent-design)'
    },
    {
        id: 'image',
        label: 'Generate Images',
        icon: 'fa-image',
        description: 'AI image generation, visual assets',
        accent: 'var(--warning)'
    },
    {
        id: 'general',
        label: 'General / Other',
        icon: 'fa-comments',
        description: 'Open-ended, let Vetinari figure it out',
        accent: 'var(--text-secondary)'
    }
];

// ── Quick-config field definitions per category ──────────────────────────

function getQuickConfigFields(category) {
    switch (category) {
        case 'code':
            return `
                <div class="qc-field">
                    <label class="qc-label" for="qcProjectName">Project Name</label>
                    <input type="text" class="input qc-input" id="qcProjectName"
                        placeholder="e.g., My Web App, Trading Bot">
                </div>
                <div class="qc-field">
                    <label class="qc-label" for="qcGoal">What do you want to build?</label>
                    <textarea class="textarea qc-textarea" id="qcGoal" rows="2"
                        placeholder="Describe what you want to build..."></textarea>
                </div>
                <div class="qc-field">
                    <label class="qc-label" for="qcTech">Languages / Frameworks</label>
                    <div class="tag-input" id="qcTagInput">
                        <div class="tag-input-tags" id="qcTagList"></div>
                        <input type="text" class="tag-input-field" id="qcTech"
                            placeholder="Type and press Enter...">
                    </div>
                </div>
                <div class="qc-field">
                    <label class="qc-label">Priority</label>
                    <div class="pill-selector" data-field="priority">
                        <button class="pill active" data-value="quality">Quality</button>
                        <button class="pill" data-value="speed">Speed</button>
                        <button class="pill" data-value="cost">Cost</button>
                    </div>
                </div>`;

        case 'research':
            return `
                <div class="qc-field">
                    <label class="qc-label" for="qcGoal">What do you want to analyze?</label>
                    <textarea class="textarea qc-textarea" id="qcGoal" rows="2"
                        placeholder="Describe what you want to research or analyze..."></textarea>
                </div>
                <div class="qc-field">
                    <label class="qc-label" for="qcSource">Source</label>
                    <input type="text" class="input qc-input" id="qcSource"
                        placeholder="File path, URL, or paste code directly in chat">
                </div>
                <div class="qc-field">
                    <label class="qc-label">Depth</label>
                    <div class="pill-selector" data-field="depth">
                        <button class="pill" data-value="quick">Quick scan</button>
                        <button class="pill active" data-value="deep">Deep analysis</button>
                        <button class="pill" data-value="comprehensive">Comprehensive</button>
                    </div>
                </div>`;

        case 'security':
            return `
                <div class="qc-field">
                    <label class="qc-label" for="qcTarget">Target</label>
                    <input type="text" class="input qc-input" id="qcTarget"
                        placeholder="File path or repository URL">
                </div>
                <div class="qc-field">
                    <label class="qc-label">Scope</label>
                    <div class="qc-checkboxes">
                        <label class="checkbox-inline"><input type="checkbox" value="owasp" checked> OWASP Top 10</label>
                        <label class="checkbox-inline"><input type="checkbox" value="full"> Full audit</label>
                        <label class="checkbox-inline"><input type="checkbox" value="specific"> Specific concerns</label>
                    </div>
                </div>
                <div class="qc-field">
                    <label class="qc-label">Severity threshold</label>
                    <div class="pill-selector" data-field="severity">
                        <button class="pill" data-value="critical">Critical only</button>
                        <button class="pill active" data-value="high">High+</button>
                        <button class="pill" data-value="all">All</button>
                    </div>
                </div>`;

        case 'data':
            return `
                <div class="qc-field">
                    <label class="qc-label" for="qcSource">Data source</label>
                    <input type="text" class="input qc-input" id="qcSource"
                        placeholder="File path, URL, or description">
                </div>
                <div class="qc-field">
                    <label class="qc-label">Task</label>
                    <div class="pill-selector" data-field="task">
                        <button class="pill active" data-value="analysis">Analysis</button>
                        <button class="pill" data-value="pipeline">Pipeline</button>
                        <button class="pill" data-value="training">Model training</button>
                        <button class="pill" data-value="visualization">Visualization</button>
                    </div>
                </div>
                <div class="qc-field">
                    <label class="qc-label">Output format</label>
                    <div class="pill-selector" data-field="output">
                        <button class="pill active" data-value="report">Report</button>
                        <button class="pill" data-value="code">Code</button>
                        <button class="pill" data-value="dashboard">Dashboard</button>
                    </div>
                </div>`;

        case 'devops':
            return `
                <div class="qc-field">
                    <label class="qc-label">Target environment</label>
                    <div class="pill-selector" data-field="environment">
                        <button class="pill" data-value="aws">AWS</button>
                        <button class="pill" data-value="gcp">GCP</button>
                        <button class="pill" data-value="azure">Azure</button>
                        <button class="pill active" data-value="docker">Docker</button>
                        <button class="pill" data-value="k8s">K8s</button>
                        <button class="pill" data-value="other">Other</button>
                    </div>
                </div>
                <div class="qc-field">
                    <label class="qc-label">Task</label>
                    <div class="pill-selector" data-field="task">
                        <button class="pill active" data-value="deploy">Deploy</button>
                        <button class="pill" data-value="monitor">Monitor</button>
                        <button class="pill" data-value="cicd">CI/CD</button>
                        <button class="pill" data-value="containerize">Containerize</button>
                    </div>
                </div>`;

        case 'creative':
            return `
                <div class="qc-field">
                    <label class="qc-label" for="qcGoal">What do you want to create?</label>
                    <textarea class="textarea qc-textarea" id="qcGoal" rows="2"
                        placeholder="Blog post, marketing copy, story, presentation..."></textarea>
                </div>
                <div class="qc-field">
                    <label class="qc-label">Content type</label>
                    <div class="pill-selector" data-field="contentType">
                        <button class="pill active" data-value="article">Article / Blog</button>
                        <button class="pill" data-value="copy">Marketing copy</button>
                        <button class="pill" data-value="story">Story / Narrative</button>
                        <button class="pill" data-value="script">Script / Dialogue</button>
                    </div>
                </div>
                <div class="qc-field">
                    <label class="qc-label">Tone</label>
                    <div class="pill-selector" data-field="tone">
                        <button class="pill" data-value="professional">Professional</button>
                        <button class="pill active" data-value="conversational">Conversational</button>
                        <button class="pill" data-value="formal">Formal</button>
                        <button class="pill" data-value="playful">Playful</button>
                    </div>
                </div>
                <div class="qc-field">
                    <label class="qc-label">Length</label>
                    <div class="pill-selector" data-field="length">
                        <button class="pill" data-value="short">Short</button>
                        <button class="pill active" data-value="medium">Medium</button>
                        <button class="pill" data-value="long">Long</button>
                    </div>
                </div>`;

        default:
            // GENERAL, DOCS, UI, IMAGE
            return `
                <div class="qc-field">
                    <label class="qc-label" for="qcGoal">What would you like to do?</label>
                    <textarea class="textarea qc-textarea" id="qcGoal" rows="3"
                        placeholder="Describe your goal..."></textarea>
                </div>
                <div class="qc-field">
                    <label class="qc-label" for="qcContext">Additional context <span class="qc-optional">(optional)</span></label>
                    <textarea class="textarea qc-textarea" id="qcContext" rows="2"
                        placeholder="Any extra details, constraints, or preferences..."></textarea>
                </div>`;
    }
}

// ── IntakeFlow class ─────────────────────────────────────────────────────

class IntakeFlow {
    constructor() {
        this._selectedCategory = null;
        this._gridEl = document.getElementById('categoryGrid');
        this._configEl = document.getElementById('quickConfigPanel');
        this._intakeForm = document.getElementById('projectIntakeForm');
        this._chatEmpty = document.getElementById('chatEmpty');
        this._chatInput = document.getElementById('chatInput');

        if (this._gridEl) {
            this._renderCategoryGrid();
            this._attachEventListeners();
        }
    }

    // ── Public API ───────────────────────────────────────────────────

    /** Show the category card grid (initial / reset state). */
    showCategories() {
        this._selectedCategory = null;
        if (this._gridEl) {
            this._gridEl.style.display = '';
            this._gridEl.classList.remove('collapsed');
        }
        if (this._configEl) {
            this._configEl.style.display = 'none';
            this._configEl.innerHTML = '';
        }
        // Hide old intake form
        if (this._intakeForm) {
            this._intakeForm.style.display = 'none';
        }
    }

    /** Select a category: collapse grid to badge, show quick-config. */
    selectCategory(categoryId) {
        const cat = CATEGORIES.find(function (c) { return c.id === categoryId; });
        if (!cat) return;

        this._selectedCategory = categoryId;

        // Collapse grid to a single badge
        if (this._gridEl) {
            this._gridEl.classList.add('collapsed');
            this._gridEl.innerHTML = `
                <button class="category-badge" id="categoryBadge" title="Change category">
                    <i class="fas ${cat.icon}"></i>
                    <span>${cat.label}</span>
                    <i class="fas fa-times category-badge-close"></i>
                </button>`;
        }

        // Show quick-config panel with category-specific fields
        if (this._configEl) {
            this._configEl.style.display = '';
            this._configEl.innerHTML = `
                <div class="quick-config-inner">
                    ${getQuickConfigFields(categoryId)}
                    <div class="qc-actions">
                        <div class="qc-model-select">
                            <label class="qc-label-inline" for="qcModelSelect">
                                <i class="fas fa-robot"></i> Model:
                            </label>
                            <select class="select qc-select" id="qcModelSelect">
                                <option value="">Auto-select best model</option>
                            </select>
                        </div>
                        <div class="qc-buttons">
                            <a href="#" class="qc-skip-link" id="qcSkipToChat">Just chat about it</a>
                            <a href="#" class="qc-advanced-link" id="qcAdvancedSetup">Advanced setup...</a>
                            <button class="btn btn-primary" id="qcLaunchBtn">
                                <i class="fas fa-rocket"></i> Launch
                            </button>
                        </div>
                    </div>
                </div>`;

            // Populate model dropdown from existing models on page
            this._populateModelSelect();
            this._attachQuickConfigListeners();
        }

        // Hide old intake form
        if (this._intakeForm) {
            this._intakeForm.style.display = 'none';
        }
    }

    /** Collect quick-config data and submit as a new project. */
    submitQuickConfig() {
        var data = this._collectQuickConfigData();
        if (!data.goal) {
            // Try to get from any qcGoal field
            var goalEl = document.getElementById('qcGoal');
            if (goalEl && goalEl.value.trim()) {
                data.goal = goalEl.value.trim();
            }
        }

        // Build a structured goal string from the collected data
        var goalParts = [];
        if (data.projectName) goalParts.push('Project: ' + data.projectName);
        goalParts.push(data.goal || 'New ' + this._selectedCategory + ' project');
        if (data.tech) goalParts.push('Tech: ' + data.tech);
        if (data.source) goalParts.push('Source: ' + data.source);
        if (data.target) goalParts.push('Target: ' + data.target);
        if (data.context) goalParts.push('Context: ' + data.context);

        // Collect pill selector values
        var pillValues = [];
        var pillSelectors = this._configEl.querySelectorAll('.pill-selector');
        pillSelectors.forEach(function (ps) {
            var field = ps.dataset.field;
            var active = ps.querySelector('.pill.active');
            if (active) pillValues.push(field + ': ' + active.dataset.value);
        });
        if (pillValues.length > 0) goalParts.push(pillValues.join(', '));

        // Collect checkbox values (security scope etc.)
        var checkboxes = this._configEl.querySelectorAll('.qc-checkboxes input:checked');
        if (checkboxes.length > 0) {
            var vals = [];
            checkboxes.forEach(function (cb) { vals.push(cb.value); });
            goalParts.push('Scope: ' + vals.join(', '));
        }

        var fullGoal = goalParts.join('\n');
        var model = document.getElementById('qcModelSelect')?.value || '';

        // Build structured metadata object for the API
        var metadata = {
            category: this._selectedCategory || 'general',
            tech_stack: data.tech || '',
            project_name: data.projectName || ''
        };

        // Collect pill selector values as structured fields
        var pillSelectors2 = this._configEl.querySelectorAll('.pill-selector');
        pillSelectors2.forEach(function (ps) {
            var field = ps.dataset.field;
            var active = ps.querySelector('.pill.active');
            if (active && field) metadata[field] = active.dataset.value;
        });

        // Collect checkbox values (e.g. security scope)
        var checkboxes2 = this._configEl.querySelectorAll('.qc-checkboxes input:checked');
        if (checkboxes2.length > 0) {
            var scopeVals = [];
            checkboxes2.forEach(function (cb) { scopeVals.push(cb.value); });
            metadata.scope = scopeVals;
        }

        // Use the existing createNewProject function from app.js
        if (typeof window.createNewProject === 'function') {
            window.createNewProject(fullGoal, model, metadata);
        } else if (typeof window.VApp !== 'undefined' && typeof window.VApp.createProject === 'function') {
            window.VApp.createProject(fullGoal, model, metadata);
        } else {
            // Fallback: put goal in chat input and trigger send
            var chatInput = document.getElementById('chatInput');
            if (chatInput) {
                chatInput.value = fullGoal;
                var sendBtn = document.getElementById('sendMessageBtn');
                if (sendBtn) sendBtn.click();
            }
        }

        // Hide the intake flow after submission
        this.hide();
    }

    /** Return to category selection (reset). */
    reset() {
        this.showCategories();
        this._renderCategoryGrid();
        this._attachEventListeners();
    }

    /** Dismiss the intake flow, let user just type in chat. */
    skipToChat() {
        this.hide();
        var chatInput = document.getElementById('chatInput');
        if (chatInput) chatInput.focus();
    }

    /** Hide the entire intake flow (called when project is active). */
    hide() {
        if (this._gridEl) this._gridEl.style.display = 'none';
        if (this._configEl) {
            this._configEl.style.display = 'none';
            this._configEl.innerHTML = '';
        }
        if (this._intakeForm) this._intakeForm.style.display = 'none';
    }

    /** Show the intake flow (called when returning to empty state). */
    show() {
        this.reset();
    }

    // ── Private ──────────────────────────────────────────────────────

    _renderCategoryGrid() {
        if (!this._gridEl) return;

        var html = '';
        CATEGORIES.forEach(function (cat) {
            html += `
                <button class="category-card" data-category="${cat.id}"
                    style="--card-accent: ${cat.accent}" tabindex="0"
                    aria-label="${cat.label}: ${cat.description}">
                    <div class="category-card-icon">
                        <i class="fas ${cat.icon}"></i>
                    </div>
                    <div class="category-card-text">
                        <span class="category-card-title">${cat.label}</span>
                        <span class="category-card-desc">${cat.description}</span>
                    </div>
                </button>`;
        });
        this._gridEl.innerHTML = html;
    }

    _attachEventListeners() {
        var self = this;
        if (!this._gridEl) return;

        this._gridEl.addEventListener('click', function (e) {
            var card = e.target.closest('.category-card');
            if (card) {
                self.selectCategory(card.dataset.category);
                return;
            }
            // Badge click to reset
            var badge = e.target.closest('.category-badge');
            if (badge) {
                self.reset();
            }
        });

        // Keyboard: Enter/Space on category cards
        this._gridEl.addEventListener('keydown', function (e) {
            if (e.key === 'Enter' || e.key === ' ') {
                var card = e.target.closest('.category-card');
                if (card) {
                    e.preventDefault();
                    self.selectCategory(card.dataset.category);
                }
            }
        });
    }

    _attachQuickConfigListeners() {
        var self = this;
        if (!this._configEl) return;

        // Pill selector click
        this._configEl.addEventListener('click', function (e) {
            var pill = e.target.closest('.pill');
            if (pill) {
                var selector = pill.closest('.pill-selector');
                if (selector) {
                    selector.querySelectorAll('.pill').forEach(function (p) {
                        p.classList.remove('active');
                    });
                    pill.classList.add('active');
                }
                return;
            }
        });

        // Launch button
        var launchBtn = document.getElementById('qcLaunchBtn');
        if (launchBtn) {
            launchBtn.addEventListener('click', function () {
                self.submitQuickConfig();
            });
        }

        // Skip to chat link
        var skipLink = document.getElementById('qcSkipToChat');
        if (skipLink) {
            skipLink.addEventListener('click', function (e) {
                e.preventDefault();
                self.skipToChat();
            });
        }

        // Tag input behavior for Languages/Frameworks
        var tagField = document.getElementById('qcTech');
        var tagList = document.getElementById('qcTagList');
        if (tagField && tagList) {
            tagField.addEventListener('keydown', function (e) {
                if (e.key === 'Enter' || e.key === ',') {
                    e.preventDefault();
                    var val = tagField.value.trim().replace(/,+$/, '').trim();
                    if (!val) return;
                    var chip = document.createElement('span');
                    chip.className = 'tag-chip';
                    chip.textContent = val;
                    var removeBtn = document.createElement('button');
                    removeBtn.className = 'tag-chip-remove';
                    removeBtn.type = 'button';
                    removeBtn.innerHTML = '<i class="fas fa-times"></i>';
                    removeBtn.addEventListener('click', function () { chip.remove(); });
                    chip.appendChild(removeBtn);
                    tagList.appendChild(chip);
                    tagField.value = '';
                }
                if (e.key === 'Backspace' && !tagField.value) {
                    var lastChip = tagList.querySelector('.tag-chip:last-child');
                    if (lastChip) lastChip.remove();
                }
            });
        }

        // Advanced setup link
        var advancedLink = document.getElementById('qcAdvancedSetup');
        if (advancedLink) {
            advancedLink.addEventListener('click', function (e) {
                e.preventDefault();
                var dialog = document.getElementById('advancedIntakeDialog');
                if (dialog && typeof dialog.showModal === 'function') {
                    dialog.showModal();
                } else if (self._intakeForm) {
                    // Fallback: show old form inline
                    self.hide();
                    self._intakeForm.style.display = '';
                }
            });
        }
    }

    _collectQuickConfigData() {
        var data = {};
        var projectName = document.getElementById('qcProjectName');
        if (projectName) data.projectName = projectName.value.trim();

        var goal = document.getElementById('qcGoal');
        if (goal) data.goal = goal.value.trim();

        var tagList = document.getElementById('qcTagList');
        var techInput = document.getElementById('qcTech');
        if (tagList) {
            var tags = [];
            tagList.querySelectorAll('.tag-chip').forEach(function (chip) {
                tags.push(chip.firstChild.textContent.trim());
            });
            // Include any text still in the input field
            if (techInput && techInput.value.trim()) {
                tags.push(techInput.value.trim());
            }
            if (tags.length > 0) data.tech = tags.join(', ');
        } else if (techInput) {
            data.tech = techInput.value.trim();
        }

        var source = document.getElementById('qcSource');
        if (source) data.source = source.value.trim();

        var target = document.getElementById('qcTarget');
        if (target) data.target = target.value.trim();

        var context = document.getElementById('qcContext');
        if (context) data.context = context.value.trim();

        data.category = this._selectedCategory;
        return data;
    }

    _populateModelSelect() {
        // Copy options from the existing chatModelSelect dropdown
        var source = document.getElementById('chatModelSelect');
        var dest = document.getElementById('qcModelSelect');
        if (!source || !dest) return;

        // Copy all options except first (keep "Auto-select")
        var opts = source.querySelectorAll('option');
        for (var i = 1; i < opts.length; i++) {
            var opt = document.createElement('option');
            opt.value = opts[i].value;
            opt.textContent = opts[i].textContent;
            dest.appendChild(opt);
        }
    }
}

// ── Initialize ───────────────────────────────────────────────────────────

var intakeFlow;

function initIntakeFlow() {
    intakeFlow = new IntakeFlow();
    window.IntakeFlow = intakeFlow;
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initIntakeFlow);
} else {
    initIntakeFlow();
}

})();
