/**
 * Code Canvas / Artifacts System for Vetinari UI.
 * Renders a slide-out panel from the right side of the screen displaying
 * syntax-highlighted code files with version history, diff view, copy,
 * download, and drag-to-resize functionality.
 *
 * Usage:
 *   CodeCanvas.open([{ filename: 'app.py', language: 'python', content: '...' }]);
 *   CodeCanvas.addVersion('app.py', '...updated content...');
 *   CodeCanvas.showDiff('app.py');
 *   CodeCanvas.close();
 *   CodeCanvas.download('app.py');
 *   CodeCanvas.downloadAll();
 */
(function() {
    'use strict';

    // ── State ──
    var _panel = null;
    var _overlay = null;
    var _files = [];           // Array of { filename, language, content }
    var _versions = {};        // Map: filename → [{ content, timestamp }]
    var _activeFile = null;    // Currently displayed filename
    var _diffMode = {};        // Map: filename → boolean
    var _previewMode = {};     // Map: filename → boolean (live preview for inert HTML/CSS only)
    var _isOpen = false;
    var _reducedMotion = false;

    var PREVIEWABLE_LANGS = ['html', 'htm', 'css'];

    var PANEL_DEFAULT_WIDTH = 520;  // px — starting width on open
    var PANEL_MIN_WIDTH = 300;      // px — drag-resize lower bound
    var PANEL_MAX_WIDTH = 1200;     // px — drag-resize upper bound
    var TRANSITION_MS = 280;        // animation duration in ms

    function escapeHtml(value) {
        return String(value == null ? '' : value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function buildPreviewDocument(fileObj) {
        var language = fileObj.language || 'text';
        var content = String(fileObj.content == null ? '' : fileObj.content);
        var csp = [
            "default-src 'none'",
            "img-src data:",
            "style-src 'unsafe-inline'",
            "script-src 'none'",
            "connect-src 'none'",
            "form-action 'none'",
            "base-uri 'none'"
        ].join('; ');

        if (language === 'css') {
            return '<!DOCTYPE html><html><head><meta http-equiv="Content-Security-Policy" content="' + csp
                + '"><style>' + content.replace(/<\/style/gi, '<\\/style') + '</style></head>'
                + '<body><p>CSS Preview</p></body></html>';
        }

        return '<!DOCTYPE html><html><head><meta http-equiv="Content-Security-Policy" content="' + csp
            + '"></head><body><pre>' + escapeHtml(content) + '</pre></body></html>';
    }

    // ── Reduced-motion preference ──
    (function detectReducedMotion() {
        if (window.matchMedia) {
            var mq = window.matchMedia('(prefers-reduced-motion: reduce)');
            _reducedMotion = mq.matches;
            mq.addEventListener('change', function(e) {
                _reducedMotion = e.matches;
            });
        }
    })();

    // ── DOM Bootstrap ──

    function ensureDOM() {
        if (_panel) return;

        // Transparent click-away overlay (does not cover panel itself)
        _overlay = document.createElement('div');
        _overlay.className = 'code-canvas-overlay';
        _overlay.addEventListener('click', close);
        document.body.appendChild(_overlay);

        // Main panel
        _panel = document.createElement('div');
        _panel.className = 'code-canvas-panel';
        _panel.setAttribute('role', 'complementary');
        _panel.setAttribute('aria-label', 'Code Canvas');
        _panel.style.width = PANEL_DEFAULT_WIDTH + 'px';
        _panel.style.transform = 'translateX(100%)';
        _panel.style.display = 'none';
        document.body.appendChild(_panel);

        // Resize handle (left edge of panel)
        var resizeHandle = document.createElement('div');
        resizeHandle.className = 'code-canvas-resize-handle';
        resizeHandle.setAttribute('aria-hidden', 'true');
        _panel.appendChild(resizeHandle);

        // Header
        var header = document.createElement('div');
        header.className = 'code-canvas-header';

        var titleEl = document.createElement('span');
        titleEl.className = 'code-canvas-title';
        titleEl.textContent = 'Code Canvas';

        var closeBtn = document.createElement('button');
        closeBtn.className = 'code-canvas-close btn btn-ghost btn-icon';
        closeBtn.setAttribute('aria-label', 'Close Code Canvas');
        closeBtn.innerHTML = '<i class="fas fa-times"></i>';
        closeBtn.addEventListener('click', close);

        header.appendChild(titleEl);
        header.appendChild(closeBtn);
        _panel.appendChild(header);

        // Tab bar (hidden when single file)
        var tabBar = document.createElement('div');
        tabBar.className = 'code-canvas-tab-bar';
        _panel.appendChild(tabBar);

        // Action bar
        var actionBar = document.createElement('div');
        actionBar.className = 'code-canvas-action-bar';
        _panel.appendChild(actionBar);

        // Code display area
        var codeArea = document.createElement('div');
        codeArea.className = 'code-canvas-code-area';
        _panel.appendChild(codeArea);

        // Wire resize handle
        wireResizeHandle(resizeHandle);

        // Inject inline styles
        injectStyles();
    }

    // ── Resize Handle ──

    function wireResizeHandle(handle) {
        var startX = 0;
        var startWidth = 0;
        var dragging = false;

        handle.addEventListener('mousedown', function(e) {
            dragging = true;
            startX = e.clientX;
            startWidth = _panel.offsetWidth;
            document.body.classList.add('code-canvas-resizing');
            e.preventDefault();
        });

        document.addEventListener('mousemove', function(e) {
            if (!dragging) return;
            // Handle is on left edge; dragging left widens the panel
            var delta = startX - e.clientX;
            var newWidth = Math.min(PANEL_MAX_WIDTH, Math.max(PANEL_MIN_WIDTH, startWidth + delta));
            _panel.style.width = newWidth + 'px';
        });

        document.addEventListener('mouseup', function() {
            if (!dragging) return;
            dragging = false;
            document.body.classList.remove('code-canvas-resizing');
        });
    }

    // ── Public API ──

    /**
     * Open the Code Canvas panel displaying the provided files.
     * @param {Array<{filename: string, language: string, content: string}>} files - Files to display.
     */
    function open(files) {
        if (!files || !files.length) return;
        ensureDOM();

        _files = files.slice();
        _activeFile = _files[0].filename;
        _diffMode = {};

        // Seed versions map — first open registers initial version
        for (var i = 0; i < _files.length; i++) {
            var f = _files[i];
            if (!_versions[f.filename]) {
                _versions[f.filename] = [];
            }
            _versions[f.filename].push({ content: f.content, timestamp: Date.now() });
        }

        renderPanel();
        showPanel();
    }

    /**
     * Store a new version of a file, enabling diff toggle for it.
     * @param {string} filename - The filename to update.
     * @param {string} newContent - New content to record.
     */
    function addVersion(filename, newContent) {
        if (!_versions[filename]) {
            _versions[filename] = [];
        }
        _versions[filename].push({ content: newContent, timestamp: Date.now() });

        // Update live content in _files array
        for (var i = 0; i < _files.length; i++) {
            if (_files[i].filename === filename) {
                _files[i].content = newContent;
                break;
            }
        }

        // Re-render if panel is open and showing this file
        if (_isOpen && _activeFile === filename) {
            renderCodeArea();
            renderActionBar();
        }
    }

    /**
     * Switch the code area to unified diff view for a file.
     * No-op if fewer than 2 versions exist.
     * @param {string} filename - The filename to diff.
     */
    function showDiff(filename) {
        if (!_versions[filename] || _versions[filename].length < 2) return;
        _diffMode[filename] = true;
        if (_isOpen && _activeFile === filename) {
            renderCodeArea();
            renderActionBar();
        }
    }

    /**
     * Close and hide the Code Canvas panel.
     */
    function close() {
        if (!_isOpen) return;
        _isOpen = false;
        _panel.classList.remove('code-canvas-panel--open');

        if (_reducedMotion) {
            _panel.style.display = 'none';
            _overlay.classList.remove('code-canvas-overlay--visible');
            return;
        }

        _panel.style.transform = 'translateX(100%)';
        _overlay.classList.remove('code-canvas-overlay--visible');

        setTimeout(function() {
            _panel.style.display = 'none';
        }, TRANSITION_MS);
    }

    /**
     * Download a single file by triggering a browser save dialog.
     * @param {string} filename - The filename to download.
     */
    function download(filename) {
        for (var i = 0; i < _files.length; i++) {
            if (_files[i].filename === filename) {
                triggerDownload(filename, _files[i].content);
                return;
            }
        }
    }

    /**
     * Download all open files. Single file triggers one download;
     * multiple files trigger individual downloads for each.
     */
    function downloadAll() {
        for (var i = 0; i < _files.length; i++) {
            triggerDownload(_files[i].filename, _files[i].content);
        }
    }

    // ── Internal Rendering ──

    function showPanel() {
        _isOpen = true;
        _panel.classList.add('code-canvas-panel--open');
        _panel.style.display = 'flex';

        if (_reducedMotion) {
            _panel.style.transform = 'translateX(0)';
            _overlay.classList.add('code-canvas-overlay--visible');
            return;
        }

        // Trigger entrance animation on next frame
        requestAnimationFrame(function() {
            requestAnimationFrame(function() {
                _panel.style.transform = 'translateX(0)';
                _overlay.classList.add('code-canvas-overlay--visible');
            });
        });
    }

    function renderPanel() {
        renderTabs();
        renderActionBar();
        renderCodeArea();
    }

    function renderTabs() {
        var tabBar = _panel.querySelector('.code-canvas-tab-bar');
        if (!tabBar) return;

        // Clear existing tabs
        while (tabBar.firstChild) {
            tabBar.removeChild(tabBar.firstChild);
        }

        if (_files.length <= 1) {
            tabBar.style.display = 'none';
            return;
        }

        tabBar.style.display = 'flex';

        for (var i = 0; i < _files.length; i++) {
            tabBar.appendChild(buildTab(_files[i]));
        }
    }

    function buildTab(file) {
        var tab = document.createElement('button');
        tab.className = 'code-canvas-tab' + (file.filename === _activeFile ? ' code-canvas-tab--active' : '');
        tab.setAttribute('type', 'button');
        tab.setAttribute('data-filename', file.filename);

        var nameSpan = document.createElement('span');
        nameSpan.className = 'code-canvas-tab-name';
        nameSpan.textContent = file.filename;

        var badge = document.createElement('span');
        badge.className = 'code-canvas-lang-badge';
        badge.textContent = file.language || 'text';

        tab.appendChild(nameSpan);
        tab.appendChild(badge);

        tab.addEventListener('click', (function(filename) {
            return function() {
                _activeFile = filename;
                _diffMode[filename] = false;
                refreshActiveTab();
                renderActionBar();
                renderCodeArea();
            };
        })(file.filename));

        return tab;
    }

    function refreshActiveTab() {
        var tabs = _panel.querySelectorAll('.code-canvas-tab');
        for (var i = 0; i < tabs.length; i++) {
            var isActive = tabs[i].getAttribute('data-filename') === _activeFile;
            tabs[i].classList.toggle('code-canvas-tab--active', isActive);
        }
    }

    function renderActionBar() {
        var actionBar = _panel.querySelector('.code-canvas-action-bar');
        if (!actionBar) return;

        while (actionBar.firstChild) {
            actionBar.removeChild(actionBar.firstChild);
        }

        if (!_activeFile) return;

        var hasVersions = _versions[_activeFile] && _versions[_activeFile].length >= 2;
        var isDiff = !!_diffMode[_activeFile];

        // Copy button
        var copyBtn = document.createElement('button');
        copyBtn.className = 'btn btn-small btn-ghost code-canvas-action-btn';
        copyBtn.setAttribute('type', 'button');
        copyBtn.innerHTML = '<i class="fas fa-copy"></i> Copy';
        copyBtn.addEventListener('click', function() { copyActive(); });
        actionBar.appendChild(copyBtn);

        // Download button
        var dlBtn = document.createElement('button');
        dlBtn.className = 'btn btn-small btn-ghost code-canvas-action-btn';
        dlBtn.setAttribute('type', 'button');
        dlBtn.innerHTML = '<i class="fas fa-download"></i> Download';
        dlBtn.addEventListener('click', function() { download(_activeFile); });
        actionBar.appendChild(dlBtn);

        // Diff toggle button — only shown when versions exist
        if (hasVersions) {
            var diffBtn = document.createElement('button');
            diffBtn.className = 'btn btn-small btn-ghost code-canvas-action-btn'
                + (isDiff ? ' code-canvas-action-btn--active' : '');
            diffBtn.setAttribute('type', 'button');
            diffBtn.innerHTML = '<i class="fas fa-code-branch"></i> '
                + (isDiff ? 'Show Code' : 'Show Diff');
            diffBtn.addEventListener('click', function() {
                if (_diffMode[_activeFile]) {
                    _diffMode[_activeFile] = false;
                } else {
                    _diffMode[_activeFile] = true;
                }
                renderActionBar();
                renderCodeArea();
            });
            actionBar.appendChild(diffBtn);
        }

        // Live preview button — only for HTML/CSS/JS files
        var fileObj = null;
        for (var fi = 0; fi < _files.length; fi++) {
            if (_files[fi].filename === _activeFile) { fileObj = _files[fi]; break; }
        }
        if (fileObj && PREVIEWABLE_LANGS.indexOf(fileObj.language) !== -1) {
            var isPreview = !!_previewMode[_activeFile];
            var previewBtn = document.createElement('button');
            previewBtn.className = 'btn btn-small btn-ghost code-canvas-action-btn'
                + (isPreview ? ' code-canvas-action-btn--active' : '');
            previewBtn.setAttribute('type', 'button');
            previewBtn.innerHTML = '<i class="fas fa-eye"></i> '
                + (isPreview ? 'Hide Preview' : 'Preview');
            previewBtn.addEventListener('click', function() {
                _previewMode[_activeFile] = !_previewMode[_activeFile];
                renderActionBar();
                renderCodeArea();
            });
            actionBar.appendChild(previewBtn);
        }
    }

    function renderCodeArea() {
        var codeArea = _panel.querySelector('.code-canvas-code-area');
        if (!codeArea) return;

        while (codeArea.firstChild) {
            codeArea.removeChild(codeArea.firstChild);
        }

        if (!_activeFile) return;

        var fileObj = null;
        for (var i = 0; i < _files.length; i++) {
            if (_files[i].filename === _activeFile) {
                fileObj = _files[i];
                break;
            }
        }
        if (!fileObj) return;

        if (_diffMode[_activeFile] && _versions[_activeFile] && _versions[_activeFile].length >= 2) {
            renderDiff(codeArea, _activeFile);
        } else {
            renderCode(codeArea, fileObj);
        }

        // Live preview iframe for HTML/CSS/JS
        if (_previewMode[_activeFile] && PREVIEWABLE_LANGS.indexOf(fileObj.language) !== -1) {
            renderPreview(codeArea, fileObj);
        }
    }

    /**
     * Render a sandboxed live preview iframe below the code.
     * Uses an empty sandbox plus a restrictive srcdoc CSP for XSS safety.
     */
    function renderPreview(container, fileObj) {
        var wrapper = document.createElement('div');
        wrapper.className = 'code-canvas-preview-wrapper';

        var label = document.createElement('div');
        label.className = 'code-canvas-preview-label';
        label.textContent = 'Live Preview';
        wrapper.appendChild(label);

        var iframe = document.createElement('iframe');
        iframe.className = 'code-canvas-preview-iframe';
        iframe.setAttribute('sandbox', '');
        iframe.setAttribute('title', 'Live preview of ' + fileObj.filename);
        wrapper.appendChild(iframe);
        container.appendChild(wrapper);
        iframe.setAttribute('srcdoc', buildPreviewDocument(fileObj));
        return;

        // Write content into iframe — wrap CSS/JS in a minimal HTML shell

        // Use srcdoc for security — avoids loading external URLs
    }

    function renderCode(container, fileObj) {
        var pre = document.createElement('pre');
        pre.className = 'code-canvas-pre';

        var code = document.createElement('code');
        // hljs uses the language class to determine highlighting
        code.className = fileObj.language ? ('language-' + fileObj.language) : '';
        code.textContent = fileObj.content;

        pre.appendChild(code);
        container.appendChild(pre);

        // Apply syntax highlighting if hljs is available
        if (window.hljs) {
            try {
                window.hljs.highlightElement(code);
            } catch (e) { /* hljs unavailable or language not supported — plain text fallback */ }
        }
    }

    function renderDiff(container, filename) {
        var versions = _versions[filename];
        var oldContent = versions[versions.length - 2].content;
        var newContent = versions[versions.length - 1].content;
        var lines = computeDiff(oldContent, newContent);

        var pre = document.createElement('pre');
        pre.className = 'code-canvas-pre code-canvas-diff';

        for (var i = 0; i < lines.length; i++) {
            var line = lines[i];
            var span = document.createElement('span');
            span.className = 'diff-line diff-line--' + line.type;

            var marker = document.createTextNode(line.type === 'added' ? '+ ' : line.type === 'removed' ? '- ' : '  ');
            span.appendChild(marker);
            span.appendChild(document.createTextNode(line.text));

            pre.appendChild(span);
            pre.appendChild(document.createTextNode('\n'));
        }

        container.appendChild(pre);
    }

    /**
     * Simple line-by-line diff — marks lines as added, removed, or same.
     * Uses a greedy LCS-style approach suitable for moderate file sizes.
     * @param {string} oldText - Original content.
     * @param {string} newText - New content.
     * @returns {Array<{type: string, text: string}>} Diff lines.
     */
    function computeDiff(oldText, newText) {
        var oldLines = oldText.split('\n');
        var newLines = newText.split('\n');
        var result = [];

        // Build LCS table
        var m = oldLines.length;
        var n = newLines.length;
        var lcs = [];
        var i, j;

        for (i = 0; i <= m; i++) {
            lcs[i] = new Array(n + 1).fill(0);
        }
        for (i = 1; i <= m; i++) {
            for (j = 1; j <= n; j++) {
                if (oldLines[i - 1] === newLines[j - 1]) {
                    lcs[i][j] = lcs[i - 1][j - 1] + 1;
                } else {
                    lcs[i][j] = Math.max(lcs[i - 1][j], lcs[i][j - 1]);
                }
            }
        }

        // Backtrack to produce diff
        var diffLines = [];
        i = m;
        j = n;
        while (i > 0 || j > 0) {
            if (i > 0 && j > 0 && oldLines[i - 1] === newLines[j - 1]) {
                diffLines.unshift({ type: 'same', text: oldLines[i - 1] });
                i--;
                j--;
            } else if (j > 0 && (i === 0 || lcs[i][j - 1] >= lcs[i - 1][j])) {
                diffLines.unshift({ type: 'added', text: newLines[j - 1] });
                j--;
            } else {
                diffLines.unshift({ type: 'removed', text: oldLines[i - 1] });
                i--;
            }
        }

        return diffLines;
    }

    // ── Copy & Download Utilities ──

    function copyActive() {
        if (!_activeFile) return;
        var content = '';
        for (var i = 0; i < _files.length; i++) {
            if (_files[i].filename === _activeFile) {
                content = _files[i].content;
                break;
            }
        }
        copyToClipboard(content);
    }

    function copyToClipboard(text) {
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text).then(function() {
                showCopyFeedback();
            }).catch(function() {
                fallbackCopy(text);
            });
        } else {
            fallbackCopy(text);
        }
    }

    function fallbackCopy(text) {
        var ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.top = '-9999px';
        ta.style.left = '-9999px';
        document.body.appendChild(ta);
        ta.focus();
        ta.select();
        try { document.execCommand('copy'); } catch (e) { /* clipboard unavailable */ }
        document.body.removeChild(ta);
        showCopyFeedback();
    }

    function showCopyFeedback() {
        if (window.ToastManager) {
            ToastManager.show('Copied to clipboard', 'success', { duration: 2000 });
        }
    }

    function triggerDownload(filename, content) {
        var blob = new Blob([content], { type: 'text/plain;charset=utf-8' });
        var url = URL.createObjectURL(blob);
        var a = document.createElement('a');
        a.href = url;
        a.download = safeDownloadName(filename);
        a.style.display = 'none';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        // Revoke after a short delay to allow the download to start
        setTimeout(function() { URL.revokeObjectURL(url); }, 1000);
    }

    function safeDownloadName(filename) {
        var base = String(filename == null ? 'artifact.txt' : filename)
            .replace(/[\\/:*?"<>|\x00-\x1f]+/g, '_')
            .replace(/^\.+/, '')
            .slice(0, 120);
        return base || 'artifact.txt';
    }

    // ── Inline Styles ──
    // Injected once so the component is self-contained without requiring a
    // separate CSS file, while still respecting the project's existing class
    // conventions and CSS variable tokens where possible.

    function injectStyles() {
        if (document.getElementById('code-canvas-styles')) return;

        var style = document.createElement('style');
        style.id = 'code-canvas-styles';
        style.textContent = [
            /* Overlay */
            '.code-canvas-overlay {',
            '  display: none;',
            '  position: fixed; inset: 0;',
            '  z-index: 1040;',
            '  background: rgba(0,0,0,0.25);',
            '}',
            '.code-canvas-overlay--visible {',
            '  display: block;',
            '}',

            /* Panel */
            '.code-canvas-panel {',
            '  position: fixed;',
            '  top: 0; right: 0; bottom: 0;',
            '  z-index: 1050;',
            '  display: flex;',
            '  flex-direction: column;',
            '  background: var(--bg-secondary, #1e1e2e);',
            '  border-left: 1px solid var(--border-color, rgba(255,255,255,0.08));',
            '  box-shadow: -4px 0 24px rgba(0,0,0,0.4);',
            '  transition: transform ' + TRANSITION_MS + 'ms cubic-bezier(0.4,0,0.2,1);',
            '  overflow: hidden;',
            '}',

            /* Resize handle */
            '.code-canvas-resize-handle {',
            '  position: absolute;',
            '  left: 0; top: 0; bottom: 0;',
            '  width: 5px;',
            '  cursor: ew-resize;',
            '  z-index: 1;',
            '}',
            '.code-canvas-resize-handle:hover, .code-canvas-resizing .code-canvas-resize-handle {',
            '  background: var(--accent-primary, #7c6af7);',
            '}',
            'body.code-canvas-resizing { cursor: ew-resize; user-select: none; }',

            /* Header */
            '.code-canvas-header {',
            '  display: flex;',
            '  align-items: center;',
            '  justify-content: space-between;',
            '  padding: 12px 16px 12px 20px;',
            '  border-bottom: 1px solid var(--border-color, rgba(255,255,255,0.08));',
            '  flex-shrink: 0;',
            '}',
            '.code-canvas-title {',
            '  font-size: 14px;',
            '  font-weight: 600;',
            '  color: var(--text-primary, #cdd6f4);',
            '  letter-spacing: 0.02em;',
            '}',
            '.code-canvas-close {',
            '  color: var(--text-muted, #6c7086);',
            '  padding: 4px 6px;',
            '}',
            '.code-canvas-close:hover { color: var(--text-primary, #cdd6f4); }',

            /* Tab bar */
            '.code-canvas-tab-bar {',
            '  display: flex;',
            '  gap: 0;',
            '  overflow-x: auto;',
            '  flex-shrink: 0;',
            '  border-bottom: 1px solid var(--border-color, rgba(255,255,255,0.08));',
            '  background: var(--bg-primary, #181825);',
            '  scrollbar-width: none;',
            '}',
            '.code-canvas-tab-bar::-webkit-scrollbar { display: none; }',
            '.code-canvas-tab {',
            '  display: inline-flex;',
            '  align-items: center;',
            '  gap: 6px;',
            '  padding: 8px 14px;',
            '  background: none;',
            '  border: none;',
            '  border-bottom: 2px solid transparent;',
            '  color: var(--text-muted, #6c7086);',
            '  font-size: 13px;',
            '  cursor: pointer;',
            '  white-space: nowrap;',
            '  transition: color 0.15s, border-color 0.15s;',
            '}',
            '.code-canvas-tab:hover { color: var(--text-primary, #cdd6f4); }',
            '.code-canvas-tab--active {',
            '  color: var(--text-primary, #cdd6f4);',
            '  border-bottom-color: var(--accent-primary, #7c6af7);',
            '  background: var(--bg-secondary, #1e1e2e);',
            '}',
            '.code-canvas-lang-badge {',
            '  font-size: 10px;',
            '  font-weight: 600;',
            '  padding: 1px 5px;',
            '  border-radius: 3px;',
            '  background: var(--bg-tertiary, #313244);',
            '  color: var(--text-muted, #6c7086);',
            '  text-transform: uppercase;',
            '  letter-spacing: 0.05em;',
            '}',

            /* Action bar */
            '.code-canvas-action-bar {',
            '  display: flex;',
            '  align-items: center;',
            '  gap: 6px;',
            '  padding: 8px 12px;',
            '  flex-shrink: 0;',
            '  border-bottom: 1px solid var(--border-color, rgba(255,255,255,0.08));',
            '  background: var(--bg-primary, #181825);',
            '}',
            '.code-canvas-action-btn {',
            '  font-size: 12px;',
            '  gap: 4px;',
            '}',
            '.code-canvas-action-btn--active {',
            '  background: var(--accent-primary, #7c6af7) !important;',
            '  color: #fff !important;',
            '  border-color: transparent !important;',
            '}',

            /* Code area */
            '.code-canvas-code-area {',
            '  flex: 1;',
            '  overflow: auto;',
            '  padding: 0;',
            '}',
            '.code-canvas-pre {',
            '  margin: 0;',
            '  padding: 16px;',
            '  font-size: 13px;',
            '  line-height: 1.6;',
            '  font-family: "JetBrains Mono", "Fira Code", Consolas, monospace;',
            '  background: transparent;',
            '  white-space: pre;',
            '  overflow-x: auto;',
            '  min-height: 100%;',
            '}',
            '.code-canvas-pre code {',
            '  background: none;',
            '  padding: 0;',
            '  font-size: inherit;',
            '}',

            /* Diff */
            '.code-canvas-diff { white-space: pre; }',
            '.diff-line { display: block; }',
            '.diff-line--added {',
            '  background: rgba(166,227,161,0.10);',
            '  color: #a6e3a1;',
            '}',
            '.diff-line--removed {',
            '  background: rgba(243,139,168,0.10);',
            '  color: #f38ba8;',
            '}',
            '.diff-line--same { color: var(--text-muted, #6c7086); }',

            /* Live preview */
            '.code-canvas-preview-wrapper {',
            '  border-top: 1px solid var(--border-default, #313244);',
            '  padding: 0;',
            '}',
            '.code-canvas-preview-label {',
            '  padding: 6px 12px;',
            '  font-size: 11px;',
            '  color: var(--text-muted, #6c7086);',
            '  text-transform: uppercase;',
            '  letter-spacing: 0.5px;',
            '  background: var(--bg-secondary, #1e1e2e);',
            '}',
            '.code-canvas-preview-iframe {',
            '  width: 100%;',
            '  height: 300px;',
            '  border: none;',
            '  background: #fff;',
            '  border-radius: 0 0 4px 4px;',
            '}',
        ].join('\n');

        document.head.appendChild(style);
    }

    // ── Init ──

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', ensureDOM);
    } else {
        ensureDOM();
    }

    window.CodeCanvas = {
        open: open,
        addVersion: addVersion,
        showDiff: showDiff,
        close: close,
        download: download,
        downloadAll: downloadAll,
        _testing: {
            buildPreviewDocument: buildPreviewDocument,
            escapeHtml: escapeHtml,
            safeDownloadName: safeDownloadName
        }
    };
})();
