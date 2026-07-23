export const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'textarea:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  '[role="button"]:not([aria-disabled="true"])',
  '[tabindex]:not([tabindex="-1"])',
].join(',');

function normalizeOptions(options = {}) {
  if (typeof options === 'boolean') {
    return { enabled: options };
  }
  return { enabled: true, restoreFocus: true, escapeEvent: 'escape', ...options };
}

function isVisible(element) {
  return element.getAttribute('aria-hidden') !== 'true' && !element.hasAttribute('hidden');
}

export function getFocusableElements(container) {
  return Array.from(container.querySelectorAll(FOCUSABLE_SELECTOR)).filter(isVisible);
}

function focusElement(element) {
  if (typeof element?.focus === 'function') {
    element.focus();
  }
}

function focusInitialElement(node, initialFocus) {
  if (typeof initialFocus === 'function') {
    focusElement(initialFocus(node));
    return;
  }

  if (typeof initialFocus === 'string') {
    focusElement(node.querySelector(initialFocus));
    return;
  }

  const [first] = getFocusableElements(node);
  focusElement(first ?? node);
}

export function focusTrap(node, options = {}) {
  let config = normalizeOptions(options);
  const previousActiveElement = document.activeElement;
  const originalTabIndex = node.getAttribute('tabindex');

  if (!node.hasAttribute('tabindex')) {
    node.setAttribute('tabindex', '-1');
  }

  function enabled() {
    return config.enabled !== false;
  }

  function handleKeydown(event) {
    if (!enabled()) return;

    if (event.key === 'Escape') {
      node.dispatchEvent(
        new CustomEvent(config.escapeEvent, {
          bubbles: true,
          cancelable: true,
          detail: { sourceEvent: event },
        }),
      );
      return;
    }

    if (event.key !== 'Tab') return;

    const focusable = getFocusableElements(node);
    if (!focusable.length) {
      event.preventDefault();
      focusElement(node);
      return;
    }

    const first = focusable[0];
    const last = focusable[focusable.length - 1];

    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      focusElement(last);
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      focusElement(first);
    }
  }

  node.addEventListener('keydown', handleKeydown);
  if (enabled()) {
    queueMicrotask(() => focusInitialElement(node, config.initialFocus));
  }

  return {
    update(nextOptions = {}) {
      config = normalizeOptions(nextOptions);
      if (enabled()) {
        queueMicrotask(() => focusInitialElement(node, config.initialFocus));
      }
    },
    destroy() {
      node.removeEventListener('keydown', handleKeydown);
      if (originalTabIndex === null) {
        node.removeAttribute('tabindex');
      } else {
        node.setAttribute('tabindex', originalTabIndex);
      }
      if (config.restoreFocus !== false && node.contains(document.activeElement)) {
        focusElement(previousActiveElement);
      }
    },
  };
}
