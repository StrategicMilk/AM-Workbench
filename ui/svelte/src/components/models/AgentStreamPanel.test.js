import { mount, unmount } from '../../../node_modules/svelte/src/index-client.js';
import { afterEach, describe, expect, it, vi } from 'vitest';

const stream = vi.hoisted(() => ({
  subscribe: vi.fn(),
  unsubscribe: vi.fn(),
  cancelGeneration: vi.fn(() => Promise.resolve({ cancelled: true })),
}));

vi.mock('$lib/stores/engineStream.svelte.js', () => stream);

import AgentStreamPanel from './AgentStreamPanel.svelte';

let component;
let container;

afterEach(async () => {
  if (component) await unmount(component);
  container?.remove();
  component = null;
  container = null;
  vi.clearAllMocks();
});

describe('AgentStreamPanel', () => {
  it('renders streamed tokens and only cancels on explicit click', async () => {
    container = document.createElement('div');
    document.body.appendChild(container);
    component = mount(AgentStreamPanel, { target: container });
    await vi.waitFor(() => expect(stream.subscribe).toHaveBeenCalledOnce());
    const handlers = stream.subscribe.mock.calls[0][0];
    handlers.message({ delta: { content: 'hello' } });
    await vi.waitFor(() => expect(container.querySelector('[aria-live="polite"]')?.textContent).toContain('hello'));
    expect(stream.cancelGeneration).not.toHaveBeenCalled();

    container.querySelector('button').click();
    await vi.waitFor(() => expect(stream.cancelGeneration).toHaveBeenCalledOnce());
    await vi.waitFor(() => expect(container.textContent).toContain('aborted'));
  });

  it('unsubscribe closes observation without issuing cancel', async () => {
    container = document.createElement('div');
    document.body.appendChild(container);
    component = mount(AgentStreamPanel, { target: container });
    await vi.waitFor(() => expect(stream.subscribe).toHaveBeenCalledOnce());
    await unmount(component);
    component = null;
    expect(stream.unsubscribe).toHaveBeenCalledOnce();
    expect(stream.cancelGeneration).not.toHaveBeenCalled();
  });
});
