/**
 * Toast Notification System for Vetinari UI.
 * Provides non-blocking feedback toasts with auto-dismiss,
 * stacking, and a special model recommendation variant.
 *
 * Usage:
 *   ToastManager.show('Saved successfully', 'success');
 *   ToastManager.show('Something went wrong', 'error', { duration: 12000 });
 *   ToastManager.showRecommendation('Llama-3-8B', 'Best fit for your GPU', '/models');
 *   ToastManager.dismiss('toast-id');
 */
(function() {
    'use strict';

    var _container = null;
    var _counter = 0;
    var _toasts = {};

    var DEFAULT_DURATION = 8000; // 8 seconds
    var ANIMATION_DURATION = 300;

    // ── Icons per toast type ──
    var ICONS = {
        info:           'fa-info-circle',
        success:        'fa-check-circle',
        warning:        'fa-exclamation-triangle',
        error:          'fa-times-circle',
        recommendation: 'fa-lightbulb'
    };

    function getContainer() {
        if (!_container) {
            _container = document.getElementById('toastContainer');
            if (!_container) {
                _container = document.createElement('div');
                _container.id = 'toastContainer';
                _container.className = 'toast-container';
                _container.setAttribute('role', 'status');
                _container.setAttribute('aria-live', 'polite');
                _container.setAttribute('aria-label', 'Notifications');
                document.body.appendChild(_container);
            }
        }
        return _container;
    }

    /**
     * Show a toast notification.
     * @param {string} message - The message to display.
     * @param {string} type - Toast type: info, success, warning, error, recommendation.
     * @param {object} [options] - Optional config.
     * @param {number} [options.duration] - Auto-dismiss duration in ms (0 = sticky).
     * @param {string} [options.action] - Action button label.
     * @param {function} [options.onAction] - Action button callback.
     * @returns {string} Toast ID for programmatic dismissal.
     */
    function show(message, type, options) {
        type = type || 'info';
        options = options || {};
        var duration = options.duration !== undefined ? options.duration : DEFAULT_DURATION;
        var id = 'toast-' + (++_counter);

        var el = document.createElement('div');
        el.className = 'toast toast--' + type;
        el.id = id;
        el.setAttribute('role', 'status');
        el.setAttribute('aria-atomic', 'true');

        var icon = ICONS[type] || ICONS.info;

        var html = '<div class="toast__icon"><i class="fas ' + icon + '"></i></div>';
        html += '<div class="toast__body">';
        html += '<div class="toast__message">' + escapeToastHtml(message) + '</div>';
        if (options.action && options.onAction) {
            html += '<button class="toast__action btn btn-small btn-ghost">' + escapeToastHtml(options.action) + '</button>';
        }
        html += '</div>';
        html += '<button class="toast__close" aria-label="Dismiss notification"><i class="fas fa-times"></i></button>';

        el.innerHTML = html;

        // Wire close button
        el.querySelector('.toast__close').addEventListener('click', function() {
            dismiss(id);
        });

        // Wire action button
        if (options.action && options.onAction) {
            el.querySelector('.toast__action').addEventListener('click', function() {
                options.onAction();
                dismiss(id);
            });
        }

        // Insert at top of container (stacks upward visually via CSS)
        var container = getContainer();
        container.insertBefore(el, container.firstChild);

        // Trigger entrance animation
        requestAnimationFrame(function() {
            el.classList.add('toast--visible');
        });

        // Auto-dismiss
        var timer = null;
        if (duration > 0) {
            timer = setTimeout(function() { dismiss(id); }, duration);
        }

        _toasts[id] = { el: el, timer: timer };
        return id;
    }

    /**
     * Show a model recommendation toast with action buttons.
     * @param {string} model - Model name/ID.
     * @param {string} reason - Why this model is recommended.
     * @param {string} downloadUrl - URL or view to navigate for download.
     * @returns {string} Toast ID.
     */
    /**
     * Show a sticky model recommendation toast with Download/Details/Dismiss.
     * @param {string} model - The recommended model name.
     * @param {string} reason - Human-readable reason for the recommendation.
     * @param {function|string} action - Callback function invoked on Download
     *     click, or a URL string (navigates to Models view). If a function,
     *     it receives no arguments and is called before the toast is dismissed.
     * @returns {string} Toast ID for programmatic dismissal.
     */
    function showRecommendation(model, reason, action) {
        var id = 'toast-' + (++_counter);

        var el = document.createElement('div');
        el.className = 'toast toast--recommendation';
        el.id = id;
        el.setAttribute('role', 'status');

        el.innerHTML = '<div class="toast__icon"><i class="fas fa-lightbulb"></i></div>'
            + '<div class="toast__body">'
            + '<div class="toast__message"><strong>Recommended: ' + escapeToastHtml(model) + '</strong></div>'
            + '<div class="toast__detail">' + escapeToastHtml(reason) + '</div>'
            + '<div class="toast__actions">'
            + '<button class="btn btn-small btn-primary toast__download">Download</button>'
            + '<button class="btn btn-small btn-ghost toast__details">Details</button>'
            + '<button class="btn btn-small btn-ghost toast__dismiss-action">Dismiss</button>'
            + '</div>'
            + '</div>'
            + '<button class="toast__close" aria-label="Dismiss notification"><i class="fas fa-times"></i></button>';

        // Wire buttons
        el.querySelector('.toast__close').addEventListener('click', function() { dismiss(id); });
        el.querySelector('.toast__dismiss-action').addEventListener('click', function() { dismiss(id); });
        el.querySelector('.toast__download').addEventListener('click', function() {
            if (typeof action === 'function') {
                action();
            } else if (action && window.VApp) {
                VApp.switchView('models');
            }
            dismiss(id);
        });
        el.querySelector('.toast__details').addEventListener('click', function() {
            if (window.VApp) VApp.switchView('models');
            dismiss(id);
        });

        var container = getContainer();
        container.insertBefore(el, container.firstChild);

        requestAnimationFrame(function() {
            el.classList.add('toast--visible');
        });

        // Recommendation toasts are sticky (no auto-dismiss) — user must interact
        _toasts[id] = { el: el, timer: null };
        return id;
    }

    /**
     * Dismiss a toast by ID.
     * @param {string} id - The toast ID returned by show().
     */
    function dismiss(id) {
        var toast = _toasts[id];
        if (!toast) return;

        if (toast.timer) clearTimeout(toast.timer);

        toast.el.classList.remove('toast--visible');
        toast.el.classList.add('toast--exiting');

        setTimeout(function() {
            if (toast.el.parentNode) {
                toast.el.parentNode.removeChild(toast.el);
            }
            delete _toasts[id];
        }, ANIMATION_DURATION);
    }

    function escapeToastHtml(str) {
        if (!str) return '';
        var div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    }

    window.ToastManager = { show: show, showRecommendation: showRecommendation, dismiss: dismiss };
})();
