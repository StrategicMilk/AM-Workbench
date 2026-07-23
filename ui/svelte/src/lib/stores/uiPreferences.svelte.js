/**
 * UI preference state for help-density controls.
 *
 * The store follows the app store pattern: private runes-backed state, public
 * getters/setters, and localStorage persistence for user-facing preferences.
 */

const STORAGE_KEYS = {
  helpDensity: 'helpDensity',
};

const HELP_DENSITIES = new Set(['compact', 'standard', 'verbose']);

/** Read a persisted string from localStorage with a default. */
function loadStored(key, fallback) {
  try {
    const raw = localStorage.getItem(key);
    return raw === null ? fallback : raw;
  } catch {
    return fallback;
  }
}

/** Persist a value to localStorage. */
function saveStored(key, value) {
  try {
    localStorage.setItem(key, String(value));
  } catch {
    // Storage can be unavailable in private browsing or SSR-like test runs.
  }
}

// Load once, validate, then pass the result to $state — avoids calling
// loadStored twice (once for the guard, once for the value).
const _storedDensity = loadStored(STORAGE_KEYS.helpDensity, 'standard');
let _helpDensity = $state(HELP_DENSITIES.has(_storedDensity) ? _storedDensity : 'standard');

export const uiPreferences = {
  get helpDensity() { return _helpDensity; },
  set helpDensity(v) {
    if (!HELP_DENSITIES.has(v)) {
      console.warn(`Invalid helpDensity "${v}"; expected compact, standard, or verbose.`);
      return;
    }
    _helpDensity = v;
    saveStored(STORAGE_KEYS.helpDensity, v);
  },
};

export const HELP_DENSITY_VALUES = ['compact', 'standard', 'verbose'];
