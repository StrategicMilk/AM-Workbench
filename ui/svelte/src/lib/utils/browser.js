export function isBrowser() {
  return typeof window !== 'undefined' && typeof document !== 'undefined';
}

export function currentLocationHref(fallback = '') {
  if (!isBrowser()) return fallback;
  return window.location?.href ?? fallback;
}

export function currentVisibilityState(fallback = 'visible') {
  if (!isBrowser()) return fallback;
  return document.visibilityState ?? fallback;
}

export function browserLocationParam(name, fallback = null) {
  if (!isBrowser() || typeof name !== 'string' || !name.trim()) return fallback;
  return new URLSearchParams(window.location.search).get(name) ?? fallback;
}
