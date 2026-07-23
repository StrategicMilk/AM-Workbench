import { announce } from './liveAnnouncement.svelte.js';

/**
 * Toast notification store.
 *
 * Push toast messages with type and auto-dismiss. The Toast component
 * reads from this store to render the notification stack.
 */

/** Auto-dismiss delay per toast type (ms). */
const DISMISS_MS = {
  info: 4000,
  success: 3000,
  warning: 5000,
  error: 8000,
};

let _nextId = 0;
let _toasts = $state([]);

/**
 * All active toasts as a snapshot — callers cannot mutate internal state.
 *
 * Returns a shallow copy so that any external `.push()` / `.splice()` on the
 * returned array does not corrupt the reactive `_toasts` list managed by
 * `showToast` and `dismissToast`.
 *
 * @returns {Array<{id: number, message: string, type: string}>}
 */
export function getToasts() {
  return [..._toasts];
}

/**
 * Show a toast notification.
 *
 * @param {string} message - Text to display.
 * @param {'info'|'success'|'warning'|'error'} [type='info'] - Severity.
 */
export function showToast(message, type = 'info') {
  const id = _nextId++;
  const safeMessage = typeof message === 'string' && message.trim() ? message.trim() : 'Notification unavailable.';
  const toast = { id, message: safeMessage, type };
  _toasts = [..._toasts, toast];
  announce(safeMessage, type === 'error' ? 'assertive' : 'polite');

  const delay = DISMISS_MS[type] ?? 4000;
  setTimeout(() => dismissToast(id), delay);
}

/**
 * Remove a toast by ID.
 * @param {number} id
 */
export function dismissToast(id) {
  _toasts = _toasts.filter((t) => t.id !== id);
}
