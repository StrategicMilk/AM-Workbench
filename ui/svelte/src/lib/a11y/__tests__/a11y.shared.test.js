import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import path from 'node:path';

import { mount, unmount } from '../../../../node_modules/svelte/src/index-client.js';
import { describe, expect, it, vi } from 'vitest';

import Icon from '../Icon.svelte';
import VisuallyHidden from '../VisuallyHidden.svelte';
import { focusTrap, getFocusableElements } from '../focusTrap.js';

const require = createRequire(import.meta.url);
const packageRoot = process.cwd();

async function renderComponent(Component, props = {}) {
  const container = document.createElement('div');
  document.body.appendChild(container);
  const component = mount(Component, { target: container, props });
  return {
    container,
    async unmount() {
      await unmount(component);
      container.remove();
    },
  };
}

describe('shared accessibility fixtures', () => {
  it('renders decorative Font Awesome icons as hidden regardless of caller input', async () => {
    const rendered = await renderComponent(Icon, {
      name: 'plus',
      class: 'toolbar-icon',
      'aria-hidden': 'false',
      title: 'ignored decorative label',
    });

    const icon = rendered.container.querySelector('i');
    expect(icon).toHaveClass('fas', 'fa-plus', 'toolbar-icon');
    expect(icon).toHaveAttribute('aria-hidden', 'true');
    expect(icon).toHaveAttribute('title', 'ignored decorative label');
    await rendered.unmount();
  });

  it('renders reusable screen-reader-only text without depending on global CSS', async () => {
    const rendered = await renderComponent(VisuallyHidden, {
      as: 'span',
      'aria-label': 'Evidence queue status',
    });

    const hidden = rendered.container.querySelector('.sr-only');
    const source = readFileSync(path.join(packageRoot, 'src/lib/a11y/VisuallyHidden.svelte'), 'utf8');
    expect(hidden).toHaveAttribute('aria-label', 'Evidence queue status');
    expect(source).toContain('position: absolute');
    expect(source).toContain('width: 1px');
    expect(source).toContain('height: 1px');
    expect(source).toContain('overflow: hidden');
    expect(source).toContain('white-space: nowrap');
    await rendered.unmount();
  });

  it('keeps keyboard focus inside overlays and emits an escape event', async () => {
    const overlay = document.createElement('section');
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-modal', 'true');
    overlay.innerHTML = `
      <button type="button" id="first">First</button>
      <a href="/" id="middle">Middle</a>
      <button type="button" id="last">Last</button>
    `;
    document.body.appendChild(overlay);
    const escapeHandler = vi.fn();
    overlay.addEventListener('escape', escapeHandler);

    const action = focusTrap(overlay);
    await Promise.resolve();

    const [first, middle, last] = getFocusableElements(overlay);
    expect(document.activeElement).toBe(first);
    expect(middle.id).toBe('middle');

    last.focus();
    const forwardTab = new KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true });
    last.dispatchEvent(forwardTab);
    expect(forwardTab.defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(first);

    const reverseTab = new KeyboardEvent('keydown', {
      key: 'Tab',
      shiftKey: true,
      bubbles: true,
      cancelable: true,
    });
    first.dispatchEvent(reverseTab);
    expect(reverseTab.defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(last);

    overlay.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    expect(escapeHandler).toHaveBeenCalledTimes(1);

    action.destroy();
    overlay.remove();
  });

  it('exports an a11y eslint profile that downstream packs can opt into', () => {
    const config = require('../../../../.eslintrc.a11y.cjs');

    expect(packageRoot).toContain('svelte');
    expect(config.extends).toEqual(['plugin:svelte/recommended', 'plugin:jsx-a11y/recommended']);
    expect(config.plugins).toEqual(['svelte', 'jsx-a11y']);
    expect(config.rules).toMatchObject({
      'jsx-a11y/aria-role': 'error',
      'jsx-a11y/interactive-supports-focus': 'error',
      'svelte/no-at-html-tags': 'error',
    });
  });
});
