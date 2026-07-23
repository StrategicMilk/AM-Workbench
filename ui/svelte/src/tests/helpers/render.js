import { createRequire } from 'node:module';

import { getQueriesForElement, waitFor } from '@testing-library/dom';
import { mount, unmount as unmountSvelte } from 'svelte';

const require = createRequire(import.meta.url);
const mountedComponents = new Set();

const appState = {
  currentView: 'dashboard',
  currentProjectId: 'test-project',
  sidebarCollapsed: false,
  commandPaletteOpen: false,
};

globalThis.vi?.mock?.('$lib/stores/app.svelte.js', () => ({
  appState,
}));

const baseAppState = Object.freeze({
  currentView: 'dashboard',
  currentProjectId: 'test-project',
  sidebarCollapsed: false,
  commandPaletteOpen: false,
});

function loadTestingLibrary() {
  try {
    return require('@testing-library/dom');
  } catch (error) {
    throw new Error(
      '@testing-library/dom is required for renderWithContext(). Install the Svelte unit-test dependencies before running component tests.',
      { cause: error },
    );
  }
}

function normalizeContext(context) {
  if (!context || context instanceof Map) {
    return context;
  }
  return new Map(Object.entries(context));
}

export function resetAppState(overrides = {}) {
  for (const key of Object.keys(appState)) {
    delete appState[key];
  }
  Object.assign(appState, baseAppState, overrides);
  return appState;
}

export function getAppState() {
  return appState;
}

export function renderWithContext(Component, props = {}, options = {}) {
  const {
    appState: appStateOverrides = {},
    baseElement = document.body,
    context,
    target,
    intro = false,
    ...mountOptions
  } = options ?? {};
  resetAppState(appStateOverrides);

  const container = target ?? document.createElement('div');
  if (!target) {
    baseElement.appendChild(container);
  }

  const component = mount(Component, {
    target: container,
    props,
    context: normalizeContext(context),
    intro,
    ...mountOptions,
  });
  const mounted = { component, container, ownedContainer: !target };
  mountedComponents.add(mounted);

  return {
    component,
    container,
    baseElement,
    ...getQueriesForElement(container),
    async unmount() {
      if (!mountedComponents.has(mounted)) {
        return;
      }
      mountedComponents.delete(mounted);
      await unmountSvelte(component);
      if (mounted.ownedContainer) {
        container.remove();
      }
    },
  };
}

export function waitForLoad(callback, options) {
  loadTestingLibrary();
  return waitFor(callback, options);
}

export async function cleanupRenderedComponents() {
  const mounted = Array.from(mountedComponents);
  mountedComponents.clear();
  await Promise.all(
    mounted.map(async ({ component, container, ownedContainer }) => {
      await unmountSvelte(component);
      if (ownedContainer) {
        container.remove();
      }
    }),
  );
}
