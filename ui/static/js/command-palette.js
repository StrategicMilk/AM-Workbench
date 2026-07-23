/**
 * Command Palette for Vetinari UI.
 * Ctrl+K (Cmd+K on Mac) opens a searchable command overlay
 * with fuzzy matching, keyboard navigation, and categories.
 *
 * Usage:
 *   CommandPalette.open();
 *   CommandPalette.close();
 *   CommandPalette.registerCommand('my-cmd', 'Do Thing', 'Ctrl+T', handler, 'Actions');
 */
(function() {
    'use strict';

    var _commands = [];
    var _backdrop = null;
    var _input = null;
    var _results = null;
    var _selectedIndex = -1;
    var _filteredCommands = [];
    var _recentIds = [];
    var MAX_RECENT = 5;
    var INPUT_ID = 'command-palette-input';
    var RESULTS_ID = 'command-palette-results';
    var OPTION_ID_PREFIX = 'command-palette-option-';

    // ── Built-in commands ──
    var BUILTINS = [
        { id: 'new-conversation',  label: 'New Conversation',  shortcut: 'Ctrl+N', category: 'Navigation', icon: 'fa-plus',        handler: function() { if (VApp) VApp.switchView('prompt'); } },
        { id: 'go-chat',           label: 'Go to Chat',        shortcut: 'Ctrl+1', category: 'Navigation', icon: 'fa-comments',    handler: function() { if (VApp) VApp.switchView('prompt'); } },
        { id: 'go-models',         label: 'Go to Models',      shortcut: 'Ctrl+2', category: 'Navigation', icon: 'fa-microchip',   handler: function() { if (VApp) VApp.switchView('models'); } },
        { id: 'go-training',       label: 'Go to Training',    shortcut: 'Ctrl+3', category: 'Navigation', icon: 'fa-graduation-cap', handler: function() { if (VApp) VApp.switchView('training'); } },
        { id: 'go-memory',         label: 'Go to Memory',      shortcut: 'Ctrl+4', category: 'Navigation', icon: 'fa-brain',       handler: function() { if (VApp) VApp.switchView('memory'); } },
        { id: 'go-settings',       label: 'Go to Settings',    shortcut: 'Ctrl+5', category: 'Navigation', icon: 'fa-cog',         handler: function() { if (VApp) VApp.switchView('settings'); } },
        { id: 'go-dashboard',      label: 'Go to Dashboard',   shortcut: 'Ctrl+6', category: 'Navigation', icon: 'fa-chart-line',  handler: function() { if (VApp) VApp.switchView('dashboard'); } },
        { id: 'toggle-theme',      label: 'Toggle Theme',      shortcut: '',        category: 'Settings',   icon: 'fa-moon',        handler: function() { if (VApp) VApp.toggleTheme(); } },
        { id: 'search-models',     label: 'Search Models...',   shortcut: '',        category: 'Models',     icon: 'fa-search',      handler: function() { if (VApp) { VApp.switchView('models'); } } },
        { id: 'search-memory',     label: 'Search Memory...',   shortcut: '',        category: 'Memory',     icon: 'fa-search',      handler: function() { if (VApp) VApp.switchView('memory'); } },
        { id: 'start-training',    label: 'Start Training',     shortcut: '',        category: 'Training',   icon: 'fa-play',        handler: function() { if (VApp) { VApp.switchView('training'); VApp.startTraining(); } } },
        { id: 'toggle-context',    label: 'Toggle Context Panel', shortcut: 'Ctrl+Shift+E', category: 'Actions', icon: 'fa-columns',  handler: function() { var p = document.getElementById('contextPanel'); if (p) p.classList.toggle('open'); } },
        { id: 'export-conversation', label: 'Export Conversation', shortcut: '',    category: 'Actions',    icon: 'fa-download',    handler: function() { if (VApp && VApp.currentProjectId) { window.open('/api/v1/chat/export/' + VApp.currentProjectId, '_blank'); } else if (window.ToastManager) { ToastManager.show('No active conversation to export', 'warning'); } } },
        { id: 'open-advanced',     label: 'Open Advanced Tools',  shortcut: '',    category: 'Actions',    icon: 'fa-tools',       handler: function() { var toggle = document.getElementById('advancedNavToggle'); if (toggle) { toggle.click(); } } },
    ];

    function init() {
        // Register built-in commands
        for (var i = 0; i < BUILTINS.length; i++) {
            var b = BUILTINS[i];
            _commands.push({
                id: b.id,
                label: b.label,
                shortcut: b.shortcut,
                category: b.category,
                icon: b.icon,
                handler: b.handler
            });
        }

        // Load recent from localStorage
        try {
            var stored = localStorage.getItem('commandPaletteRecent');
            if (stored) _recentIds = JSON.parse(stored);
        } catch (e) { /* ignore */ }

        // Create DOM
        createDOM();

        // Global keyboard shortcut
        document.addEventListener('keydown', function(e) {
            // Ctrl+K or Cmd+K
            if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
                e.preventDefault();
                open();
                return;
            }

            // Escape to close
            if (e.key === 'Escape' && isOpen()) {
                e.preventDefault();
                close();
                return;
            }

            // Navigation shortcuts (only when palette is closed)
            if (!isOpen() && (e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey) {
                var num = parseInt(e.key);
                if (num >= 1 && num <= 6) {
                    var views = ['prompt', 'models', 'training', 'memory', 'settings', 'dashboard'];
                    e.preventDefault();
                    if (VApp) VApp.switchView(views[num - 1]);
                }
            }
        });
    }

    function createDOM() {
        _backdrop = document.createElement('div');
        _backdrop.className = 'command-palette-backdrop';
        _backdrop.innerHTML =
            '<div class="command-palette" role="dialog" aria-label="Command palette">'
            + '<div class="command-palette__input-wrap">'
            + '<i class="fas fa-search command-palette__icon"></i>'
            + '<input id="' + INPUT_ID + '" class="command-palette__input" type="text" role="combobox" aria-autocomplete="list" aria-controls="' + RESULTS_ID + '" aria-expanded="false" aria-activedescendant="" placeholder="Type a command or search..." autocomplete="off" spellcheck="false">'
            + '<span class="command-palette__shortcut-hint">Esc</span>'
            + '</div>'
            + '<div id="' + RESULTS_ID + '" class="command-palette__results" role="listbox" aria-labelledby="' + INPUT_ID + '"></div>'
            + '</div>';

        document.body.appendChild(_backdrop);

        _input = _backdrop.querySelector('.command-palette__input');
        _results = _backdrop.querySelector('.command-palette__results');

        // Close on backdrop click
        _backdrop.addEventListener('click', function(e) {
            if (e.target === _backdrop) close();
        });

        // Input handler
        _input.addEventListener('input', function() {
            search(_input.value);
        });

        // Keyboard navigation
        _input.addEventListener('keydown', function(e) {
            if (e.key === 'ArrowDown') {
                e.preventDefault();
                selectNext();
            } else if (e.key === 'ArrowUp') {
                e.preventDefault();
                selectPrev();
            } else if (e.key === 'Enter') {
                e.preventDefault();
                executeSelected();
            }
        });
    }

    function open() {
        if (!_backdrop) return;
        _backdrop.classList.add('visible');
        _input.value = '';
        _selectedIndex = -1;
        // Show recent or all commands
        search('');
        _input.focus();
    }

    function close() {
        if (!_backdrop) return;
        _backdrop.classList.remove('visible');
        _input.value = '';
        updateComboboxState();
    }

    function isOpen() {
        return _backdrop && _backdrop.classList.contains('visible');
    }

    /**
     * Register a custom command.
     * @param {string} id - Unique command ID.
     * @param {string} label - Display label.
     * @param {string} shortcut - Keyboard shortcut hint.
     * @param {function} handler - Callback when executed.
     * @param {string} category - Category for grouping.
     */
    function registerCommand(id, label, shortcut, handler, category) {
        // Prevent duplicates
        for (var i = 0; i < _commands.length; i++) {
            if (_commands[i].id === id) {
                _commands[i] = { id: id, label: label, shortcut: shortcut || '', handler: handler, category: category || 'Actions', icon: 'fa-terminal' };
                return;
            }
        }
        _commands.push({
            id: id,
            label: label,
            shortcut: shortcut || '',
            handler: handler,
            category: category || 'Actions',
            icon: 'fa-terminal'
        });
    }

    function search(query) {
        query = query.trim().toLowerCase();

        if (!query) {
            // Show recent + all
            var recent = [];
            for (var r = 0; r < _recentIds.length && r < MAX_RECENT; r++) {
                for (var c = 0; c < _commands.length; c++) {
                    if (_commands[c].id === _recentIds[r]) {
                        recent.push(_commands[c]);
                        break;
                    }
                }
            }
            _filteredCommands = recent.length > 0 ? recent.concat(_commands) : _commands;
        } else {
            // Fuzzy filter
            _filteredCommands = [];
            for (var i = 0; i < _commands.length; i++) {
                var cmd = _commands[i];
                if (fuzzyMatch(query, cmd.label.toLowerCase()) || fuzzyMatch(query, cmd.category.toLowerCase())) {
                    _filteredCommands.push(cmd);
                }
            }
        }

        _selectedIndex = _filteredCommands.length > 0 ? 0 : -1;
        renderResults(query ? null : 'Recent');
    }

    function fuzzyMatch(query, text) {
        var qi = 0;
        for (var ti = 0; ti < text.length && qi < query.length; ti++) {
            if (text[ti] === query[qi]) qi++;
        }
        return qi === query.length;
    }

    function renderResults(recentLabel) {
        if (!_results) return;

        if (_filteredCommands.length === 0) {
            _results.innerHTML = '<div class="command-palette__empty">No matching commands</div>';
            updateComboboxState();
            return;
        }

        var html = '';
        var lastCategory = '';
        var shownRecent = false;

        for (var i = 0; i < _filteredCommands.length; i++) {
            var cmd = _filteredCommands[i];

            // Category header
            if (recentLabel && !shownRecent && i < _recentIds.length) {
                if (i === 0) {
                    html += '<div class="command-palette__category">Recent</div>';
                }
                if (i === _recentIds.length) {
                    shownRecent = true;
                    html += '<div class="command-palette__category">All Commands</div>';
                    lastCategory = '';
                }
            } else if (cmd.category !== lastCategory) {
                html += '<div class="command-palette__category">' + escapeHtml(cmd.category) + '</div>';
                lastCategory = cmd.category;
            }

            var selected = i === _selectedIndex ? ' selected' : '';
            html += '<div id="' + OPTION_ID_PREFIX + i + '" class="command-palette__item' + selected + '" data-index="' + i + '" role="option" aria-selected="' + (i === _selectedIndex ? 'true' : 'false') + '">'
                + '<div class="command-palette__item-icon"><i class="fas ' + (cmd.icon || 'fa-terminal') + '"></i></div>'
                + '<div class="command-palette__item-label">' + escapeHtml(cmd.label) + '</div>'
                + (cmd.shortcut ? '<div class="command-palette__item-shortcut">' + escapeHtml(cmd.shortcut) + '</div>' : '')
                + '</div>';
        }

        _results.innerHTML = html;

        // Wire click handlers
        var items = _results.querySelectorAll('.command-palette__item');
        for (var j = 0; j < items.length; j++) {
            items[j].addEventListener('click', (function(idx) {
                return function() {
                    _selectedIndex = idx;
                    executeSelected();
                };
            })(j));
            items[j].addEventListener('mouseenter', (function(idx) {
                return function() {
                    _selectedIndex = idx;
                    updateSelection();
                };
            })(j));
        }
        updateSelection();
    }

    function selectNext() {
        if (_filteredCommands.length === 0) return;
        _selectedIndex = (_selectedIndex + 1) % _filteredCommands.length;
        updateSelection();
    }

    function selectPrev() {
        if (_filteredCommands.length === 0) return;
        _selectedIndex = (_selectedIndex - 1 + _filteredCommands.length) % _filteredCommands.length;
        updateSelection();
    }

    function updateSelection() {
        if (!_results) return;
        var items = _results.querySelectorAll('.command-palette__item');
        for (var i = 0; i < items.length; i++) {
            items[i].classList.toggle('selected', i === _selectedIndex);
            items[i].setAttribute('aria-selected', i === _selectedIndex ? 'true' : 'false');
        }
        updateComboboxState();
        // Scroll into view
        if (items[_selectedIndex]) {
            items[_selectedIndex].scrollIntoView({ block: 'nearest' });
        }
    }

    function updateComboboxState() {
        if (!_input) return;
        var expanded = isOpen() && _filteredCommands.length > 0;
        _input.setAttribute('aria-expanded', expanded ? 'true' : 'false');
        if (expanded && _selectedIndex >= 0 && _selectedIndex < _filteredCommands.length) {
            _input.setAttribute('aria-activedescendant', OPTION_ID_PREFIX + _selectedIndex);
        } else {
            _input.setAttribute('aria-activedescendant', '');
        }
    }

    function executeSelected() {
        if (_selectedIndex < 0 || _selectedIndex >= _filteredCommands.length) return;
        var cmd = _filteredCommands[_selectedIndex];

        // Track recent
        _recentIds = _recentIds.filter(function(id) { return id !== cmd.id; });
        _recentIds.unshift(cmd.id);
        if (_recentIds.length > MAX_RECENT) _recentIds = _recentIds.slice(0, MAX_RECENT);
        try { localStorage.setItem('commandPaletteRecent', JSON.stringify(_recentIds)); } catch (e) { /* ignore */ }

        close();
        if (cmd.handler) cmd.handler();
    }

    function escapeHtml(str) {
        if (!str) return '';
        var div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    }

    // Initialize on DOMContentLoaded
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    window.CommandPalette = {
        open: open,
        close: close,
        registerCommand: registerCommand
    };
})();
