/**
 * Defensive UI primitives for data that crosses runtime/API boundaries.
 *
 * These helpers keep Svelte components from treating missing, malformed, or
 * stale payload fields as successful runtime signal.
 */

export function asArray(value) {
  return Array.isArray(value) ? value : [];
}

export function finiteNumber(value, fallback = null) {
  const number = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(number) ? number : fallback;
}

export function clampPercent(value, fallback = null) {
  const number = finiteNumber(value, fallback);
  if (number === fallback) return fallback;
  return Math.max(0, Math.min(100, number));
}

export function clampUnit(value, fallback = null) {
  const number = finiteNumber(value, fallback);
  if (number === fallback) return fallback;
  return Math.max(0, Math.min(1, number));
}

export function unitPercent(value, fallback = 0) {
  const number = clampUnit(value, null);
  if (number === null) return fallback;
  return Math.round(number * 100);
}

export function nonEmptyString(value, fallback = 'unknown') {
  return typeof value === 'string' && value.trim() ? value.trim() : fallback;
}

export function errorMessage(error, fallback = 'Operation failed.') {
  if (error instanceof Error && error.message.trim()) return error.message;
  if (typeof error === 'string' && error.trim()) return error.trim();
  if (error && typeof error === 'object' && typeof error.message === 'string' && error.message.trim()) {
    return error.message.trim();
  }
  return fallback;
}
