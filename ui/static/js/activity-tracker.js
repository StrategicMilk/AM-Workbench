/**
 * Activity & Progress Tracker for Vetinari UI.
 * Tracks in-progress operations with progress bars, ETAs,
 * and a persistent activity bar at the bottom of the viewport.
 *
 * Usage:
 *   ActivityTracker.startActivity('dl-model', 'Downloading Llama-3', { total: 100 });
 *   ActivityTracker.updateProgress('dl-model', 45, 'Downloading layer 3/7');
 *   ActivityTracker.updateETA('dl-model', 120);
 *   ActivityTracker.completeActivity('dl-model', 'Download complete');
 *   ActivityTracker.failActivity('dl-model', 'Connection timeout');
 */
(function() {
    'use strict';

    var _activities = {};
    var _bar = null;
    var _completeDismissDelay = 3000; // 3s dismiss for completed activities

    function clampProgressPercent(value) {
        if (value == null) return null;
        var numeric = Number(value);
        if (!Number.isFinite(numeric)) return null;
        return Math.min(100, Math.max(0, numeric));
    }

    // ── DOM References ──
    function getBar() {
        if (!_bar) {
            _bar = document.getElementById('activityBar');
        }
        return _bar;
    }

    /**
     * Start tracking a new activity.
     * @param {string} id - Unique activity identifier.
     * @param {string} label - Human-readable label.
     * @param {object} [options] - Optional config.
     * @param {number} [options.total] - Total steps (for step-based progress).
     * @param {number} [options.step] - Current step number.
     * @returns {string} The activity ID.
     */
    function startActivity(id, label, options) {
        options = options || {};
        _activities[id] = {
            id: id,
            label: label,
            status: 'active',
            percent: null,
            detail: '',
            eta: null,
            startTime: Date.now(),
            step: options.step || null,
            total: options.total || null,
            summary: ''
        };
        render();
        return id;
    }

    /**
     * Update progress for an activity.
     * @param {string} id - Activity ID.
     * @param {number|null} percent - Progress percentage (0-100), null for indeterminate.
     * @param {string} [detail] - Granular status text.
     */
    function updateProgress(id, percent, detail) {
        var activity = _activities[id];
        if (!activity || activity.status !== 'active') return;
        activity.percent = clampProgressPercent(percent);
        if (detail !== undefined) activity.detail = detail;
        render();
    }

    /**
     * Update the estimated time remaining.
     * @param {string} id - Activity ID.
     * @param {number} estimatedSeconds - Seconds remaining.
     */
    function updateETA(id, estimatedSeconds) {
        var activity = _activities[id];
        if (!activity || activity.status !== 'active') return;
        activity.eta = estimatedSeconds;
        render();
    }

    /**
     * Mark an activity as completed.
     * @param {string} id - Activity ID.
     * @param {string} [summary] - Completion summary text.
     */
    function completeActivity(id, summary) {
        var activity = _activities[id];
        if (!activity) return;
        activity.status = 'complete';
        activity.percent = 100;
        activity.summary = summary || 'Done';
        activity.detail = summary || 'Done';
        activity.label = summary || 'Done';
        activity.eta = null;
        render();

        // Auto-dismiss after delay
        setTimeout(function() {
            delete _activities[id];
            render();
        }, _completeDismissDelay);
    }

    /**
     * Mark an activity as failed.
     * @param {string} id - Activity ID.
     * @param {string} error - Error description.
     */
    function failActivity(id, error) {
        var activity = _activities[id];
        if (!activity) return;
        activity.status = 'failed';
        activity.detail = error || 'Failed';
        activity.eta = null;
        render();
    }

    /**
     * Get all currently tracked activities.
     * @returns {Array} Array of activity objects.
     */
    function getActiveActivities() {
        var result = [];
        for (var key in _activities) {
            if (_activities[key].status === 'active') {
                result.push(_activities[key]);
            }
        }
        return result;
    }

    // ── Rendering ──

    function render() {
        var bar = getBar();
        if (!bar) return;

        var keys = Object.keys(_activities);
        var hasActivities = keys.length > 0;

        if (hasActivities) {
            bar.classList.add('has-activities');
        } else {
            bar.classList.remove('has-activities');
            bar.classList.remove('expanded');
            // Reset the label text when no activities remain
            var emptyLabel = bar.querySelector('.activity-bar__collapsed .activity-bar__label');
            if (emptyLabel) emptyLabel.textContent = 'No active tasks';
            var emptyFill = bar.querySelector('.activity-bar__collapsed .activity-bar__mini-fill');
            if (emptyFill) emptyFill.style.width = '0%';
            var emptyPulse = bar.querySelector('.activity-bar__collapsed .activity-bar__pulse');
            if (emptyPulse) emptyPulse.style.display = 'none';
            return;
        }

        // Find primary active activity for collapsed view
        var primary = null;
        var activeCount = 0;
        for (var i = 0; i < keys.length; i++) {
            if (_activities[keys[i]].status === 'active') {
                if (!primary) primary = _activities[keys[i]];
                activeCount++;
            }
        }
        if (!primary && keys.length > 0) {
            primary = _activities[keys[keys.length - 1]];
        }

        // Collapsed view
        var collapsed = bar.querySelector('.activity-bar__collapsed');
        if (collapsed && primary) {
            var labelEl = collapsed.querySelector('.activity-bar__label');
            var fillEl = collapsed.querySelector('.activity-bar__mini-fill');
            var pulseEl = collapsed.querySelector('.activity-bar__pulse');

            if (labelEl) {
                var text = primary.label;
                if (activeCount > 1) text += ' (+' + (activeCount - 1) + ' more)';
                if (primary.detail) text += ' — ' + primary.detail;
                labelEl.textContent = text;
            }
            if (fillEl) {
                fillEl.style.width = (primary.percent != null ? primary.percent : 0) + '%';
            }
            if (pulseEl) {
                pulseEl.style.display = primary.status === 'active' ? '' : 'none';
            }
        }

        // Expanded view
        var expanded = bar.querySelector('.activity-bar__expanded');
        if (expanded) {
            var html = '';
            for (var j = 0; j < keys.length; j++) {
                var a = _activities[keys[j]];
                html += renderActivityItem(a);
            }
            expanded.innerHTML = html;
        }
    }

    function renderActivityItem(a) {
        var iconClass, iconEl;
        if (a.status === 'active') {
            iconClass = 'spinning';
            iconEl = '<i class="fas fa-spinner"></i>';
        } else if (a.status === 'complete') {
            iconClass = 'complete';
            iconEl = '<i class="fas fa-check"></i>';
        } else {
            iconClass = 'failed';
            iconEl = '<i class="fas fa-times"></i>';
        }

        var elapsed = formatElapsed(Date.now() - a.startTime);
        var etaText = a.eta ? '~' + formatElapsed(a.eta * 1000) + ' remaining' : '';

        var progressHtml = '';
        if (a.status === 'active') {
            if (a.percent != null) {
                progressHtml = '<div class="progress-bar"><div class="progress-bar__fill" style="width:' + clampProgressPercent(a.percent) + '%"></div></div>';
            } else {
                progressHtml = '<div class="progress-bar indeterminate"><div class="progress-bar__fill"></div></div>';
            }
        }

        return '<div class="activity-item">'
            + '<div class="activity-item__icon ' + iconClass + '">' + iconEl + '</div>'
            + '<div class="activity-item__body">'
            + '<div class="activity-item__label">' + escapeHtml(a.label) + '</div>'
            + (a.detail ? '<div class="activity-item__detail">' + escapeHtml(a.detail) + '</div>' : '')
            + progressHtml
            + '<div class="activity-item__meta">'
            + '<span>' + elapsed + ' elapsed</span>'
            + (etaText ? '<span>' + etaText + '</span>' : '')
            + '</div>'
            + '</div>'
            + '</div>';
    }

    function formatElapsed(ms) {
        var s = Math.floor(ms / 1000);
        if (s < 60) return s + 's';
        var m = Math.floor(s / 60);
        s = s % 60;
        if (m < 60) return m + 'm ' + s + 's';
        var h = Math.floor(m / 60);
        m = m % 60;
        return h + 'h ' + m + 'm';
    }

    function escapeHtml(str) {
        if (!str) return '';
        var div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    }

    // ── Toggle expand/collapse ──
    document.addEventListener('DOMContentLoaded', function() {
        var bar = getBar();
        if (!bar) return;

        var collapsed = bar.querySelector('.activity-bar__collapsed');
        if (collapsed) {
            collapsed.addEventListener('click', function() {
                bar.classList.toggle('expanded');
            });
        }

        // Pipeline node click handlers — open context panel with agent logs
        var pipelineNodes = document.querySelectorAll('.pipeline-node');
        for (var i = 0; i < pipelineNodes.length; i++) {
            pipelineNodes[i].addEventListener('click', function(e) {
                var node = e.currentTarget;
                var agentName = node.getAttribute('data-agent') || 'unknown';
                document.dispatchEvent(new CustomEvent('vetinari:pipelineNodeClick', {
                    detail: { agent: agentName, nodeId: node.id }
                }));
                // Open context panel and show agent info
                var contextPanel = document.getElementById('contextPanel');
                if (contextPanel) {
                    contextPanel.classList.add('open');
                    var panelBody = contextPanel.querySelector('.context-panel-body');
                    if (panelBody) {
                        var modeEl = node.querySelector('.pipeline-node__mode');
                        var timeEl = node.querySelector('.pipeline-node__time');
                        var mode = modeEl ? modeEl.textContent : 'idle';
                        var time = timeEl ? timeEl.textContent : '--';
                        var statusClass = node.classList.contains('pipeline-node--active') ? 'Running' :
                            node.classList.contains('pipeline-node--completed') ? 'Completed' :
                            node.classList.contains('pipeline-node--failed') ? 'Failed' : 'Idle';
                        panelBody.innerHTML =
                            '<div class="context-agent-info">' +
                            '<h3>' + agentName.charAt(0).toUpperCase() + agentName.slice(1) + ' Agent</h3>' +
                            '<div class="context-agent-detail"><strong>Status:</strong> ' + statusClass + '</div>' +
                            '<div class="context-agent-detail"><strong>Mode:</strong> ' + escapeHtml(mode) + '</div>' +
                            '<div class="context-agent-detail"><strong>Elapsed:</strong> ' + escapeHtml(time) + '</div>' +
                            '</div>';
                    }
                }
            });
        }
    });

    // ── Pipeline Node Graph Integration ──

    /**
     * Agent name to pipeline node ID mapping.
     */
    var AGENT_NODE_MAP = {
        foreman: 'pipelineForeman',
        worker: 'pipelineWorker',
        inspector: 'pipelineInspector'
    };

    /**
     * Update a pipeline node's visual state.
     * @param {string} agent - Agent name (foreman, worker, inspector).
     * @param {string} state - One of: active, completed, failed, idle.
     * @param {object} [info] - Optional {mode, time} info.
     */
    function updatePipelineNode(agent, state, info) {
        var nodeId = AGENT_NODE_MAP[agent];
        if (!nodeId) return;

        var node = document.getElementById(nodeId);
        if (!node) return;

        var graph = document.getElementById('pipelineGraph');
        if (graph) graph.style.display = 'flex';

        // Remove all state classes
        node.classList.remove('pipeline-node--active', 'pipeline-node--completed', 'pipeline-node--failed');

        if (state === 'active') {
            node.classList.add('pipeline-node--active');
            // Animate the edge leading TO this node
            activateEdgeBefore(node);
        } else if (state === 'completed') {
            node.classList.add('pipeline-node--completed');
            deactivateEdgeBefore(node);
        } else if (state === 'failed') {
            node.classList.add('pipeline-node--failed');
            deactivateEdgeBefore(node);
        } else {
            deactivateEdgeBefore(node);
        }

        // Update mode and time text
        info = info || {};
        var modeEl = node.querySelector('.pipeline-node__mode');
        var timeEl = node.querySelector('.pipeline-node__time');
        if (modeEl && info.mode) modeEl.textContent = info.mode;
        if (timeEl && info.time !== undefined) timeEl.textContent = info.time;
    }

    function activateEdgeBefore(node) {
        var edge = node.previousElementSibling;
        if (edge && edge.classList.contains('pipeline-edge')) {
            edge.classList.add('pipeline-edge--active');
        }
    }

    function deactivateEdgeBefore(node) {
        var edge = node.previousElementSibling;
        if (edge && edge.classList.contains('pipeline-edge')) {
            edge.classList.remove('pipeline-edge--active');
        }
    }

    /**
     * Reset all pipeline nodes to idle.
     */
    function resetPipeline() {
        for (var agent in AGENT_NODE_MAP) {
            var node = document.getElementById(AGENT_NODE_MAP[agent]);
            if (node) {
                node.classList.remove('pipeline-node--active', 'pipeline-node--completed', 'pipeline-node--failed');
                var modeEl = node.querySelector('.pipeline-node__mode');
                var timeEl = node.querySelector('.pipeline-node__time');
                if (modeEl) modeEl.textContent = 'idle';
                if (timeEl) timeEl.textContent = '--';
            }
        }
        var edges = document.querySelectorAll('.pipeline-edge');
        for (var i = 0; i < edges.length; i++) {
            edges[i].classList.remove('pipeline-edge--active');
        }
    }

    window.ActivityTracker = {
        startActivity: startActivity,
        updateProgress: updateProgress,
        updateETA: updateETA,
        completeActivity: completeActivity,
        failActivity: failActivity,
        getActiveActivities: getActiveActivities,
        updatePipelineNode: updatePipelineNode,
        resetPipeline: resetPipeline
    };
})();
