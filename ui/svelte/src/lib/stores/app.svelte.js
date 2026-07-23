/**
 * Central application state using Svelte 5 runes.
 *
 * Single reactive store for global UI state. Persists select keys to
 * localStorage so preferences survive page reloads.
 */

const STORAGE_KEYS = {
  theme: 'theme',
  sidebarCollapsed: 'sidebarCollapsed',
  setupComplete: 'setupComplete',
  focusMode: 'focusModeEnabled',
};

const VIEW_ALIASES = {
  workbench_extensions: 'workbench-extensions',
};

/** Read a boolean or string from localStorage with a default. */
function loadStored(key, fallback) {
  try {
    const raw = localStorage.getItem(key);
    if (raw === null) return fallback;
    if (raw === 'true') return true;
    if (raw === 'false') return false;
    return raw;
  } catch {
    return fallback;
  }
}

/** Persist a value to localStorage. */
function saveStored(key, value) {
  try {
    localStorage.setItem(key, String(value));
  } catch {
    // Storage full or unavailable — degrade silently
  }
}

function defaultTheme() {
  try {
    return window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
  } catch {
    return 'dark';
  }
}

function normalizeView(view) {
  return VIEW_ALIASES[view] ?? view;
}

// -- Reactive state ----------------------------------------------------------

// FSA-0056: read setupComplete from storage FIRST so we can route the very
// first paint to the onboarding view if the user hasn't completed first-run
// setup yet.  resolveDefaultLandingView lives in routes/defaultLanding.js
// and returns 'onboarding' when setupComplete is falsy, 'prompt' otherwise.
const _initialSetupComplete = loadStored(STORAGE_KEYS.setupComplete, false);

function initialView(setupComplete) {
  // Use the stored currentView if one exists; fall back to the
  // setup-aware default when there's no prior view.
  const stored = loadStored('currentView', null);
  if (stored != null) return normalizeView(stored);
  return setupComplete === true ? 'prompt' : 'onboarding';
}

let _currentView = $state(initialView(_initialSetupComplete));
let _currentProjectId = $state(null);
let _sidebarCollapsed = $state(loadStored(STORAGE_KEYS.sidebarCollapsed, false));
let _theme = $state(loadStored(STORAGE_KEYS.theme, defaultTheme()));
let _commandPaletteOpen = $state(false);
let _commandPaletteQuery = $state('');
let _setupComplete = $state(_initialSetupComplete);
let _focusMode = $state(loadStored(STORAGE_KEYS.focusMode, false));
// Starts false — shell shows Disconnected until SSE handshakes and sets this true.
let _serverConnected = $state(false);
let _sessionTokens = $state(0);

/**
 * Reactive application state object.
 *
 * Access and mutate properties directly — Svelte 5 runes handle reactivity.
 * Persisted keys auto-sync to localStorage on write.
 */
export const appState = {
  get currentView() { return _currentView; },
  set currentView(v) {
    const normalizedView = normalizeView(v);
    _currentView = normalizedView;
    saveStored('currentView', normalizedView);
  },

  get currentProjectId() { return _currentProjectId; },
  set currentProjectId(v) { _currentProjectId = v; },

  get sidebarCollapsed() { return _sidebarCollapsed; },
  set sidebarCollapsed(v) {
    _sidebarCollapsed = v;
    saveStored(STORAGE_KEYS.sidebarCollapsed, v);
  },

  get theme() { return _theme; },
  set theme(v) {
    _theme = v;
    saveStored(STORAGE_KEYS.theme, v);
  },

  get commandPaletteOpen() { return _commandPaletteOpen; },
  set commandPaletteOpen(v) { _commandPaletteOpen = v; },

  get commandPaletteQuery() { return _commandPaletteQuery; },
  set commandPaletteQuery(v) { _commandPaletteQuery = String(v ?? ''); },

  get setupComplete() { return _setupComplete; },
  set setupComplete(v) {
    _setupComplete = v;
    saveStored(STORAGE_KEYS.setupComplete, v);
  },

  get focusMode() { return _focusMode; },
  set focusMode(v) {
    _focusMode = v;
    saveStored(STORAGE_KEYS.focusMode, v);
  },

  get serverConnected() { return _serverConnected; },
  set serverConnected(v) { _serverConnected = v; },

  get sessionTokens() { return _sessionTokens; },
  set sessionTokens(v) { _sessionTokens = v; },
};
