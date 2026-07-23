export const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'textarea:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  '[contenteditable="true"]',
  '[role="button"]:not([aria-disabled="true"])',
  '[tabindex]:not([tabindex="-1"])',
].join(',');

export function getFocusableElements(container: HTMLElement): HTMLElement[] {
  return Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).filter(
    (element) => !element.hasAttribute('disabled') && element.getAttribute('aria-hidden') !== 'true'
  );
}

export function focusFirstElement(container: HTMLElement): void {
  const [first] = getFocusableElements(container);
  (first ?? container).focus();
}

export function trapFocus(node: HTMLElement, enabled = true) {
  let active = enabled;

  function handleKeydown(event: KeyboardEvent) {
    if (!active || event.key !== 'Tab') return;
    const focusable = getFocusableElements(node);
    if (!focusable.length) {
      event.preventDefault();
      node.focus();
      return;
    }

    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  node.addEventListener('keydown', handleKeydown);
  if (active) queueMicrotask(() => focusFirstElement(node));

  return {
    update(value = true) {
      active = value;
      if (active) queueMicrotask(() => focusFirstElement(node));
    },
    destroy() {
      node.removeEventListener('keydown', handleKeydown);
    },
  };
}
