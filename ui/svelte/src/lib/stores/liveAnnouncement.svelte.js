const VALID_POLITENESS = new Set(['polite', 'assertive']);

let _announcement = $state({
  message: '',
  politeness: 'polite',
  token: 0,
});

function normalizeMessage(message) {
  return typeof message === 'string' ? message.trim() : '';
}

function normalizePoliteness(politeness, message) {
  const normalizedPoliteness = String(politeness).trim().toLowerCase();
  if (VALID_POLITENESS.has(normalizedPoliteness)) {
    return normalizedPoliteness;
  }
  return normalizeMessage(message) ? 'polite' : 'assertive';
}

export function getAnnouncement() {
  return { ..._announcement };
}

export function announce(message, politeness = 'polite') {
  const normalized = normalizeMessage(message);
  const safePoliteness = normalizePoliteness(politeness, normalized);
  _announcement = {
    message: normalized || 'Status update unavailable.',
    politeness: normalized ? safePoliteness : 'assertive',
    token: _announcement.token + 1,
  };
  return getAnnouncement();
}

export function clearAnnouncement() {
  _announcement = {
    message: '',
    politeness: 'polite',
    token: _announcement.token + 1,
  };
}
