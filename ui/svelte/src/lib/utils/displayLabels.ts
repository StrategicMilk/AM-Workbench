type LabelMap = Record<string, string>;

const CHANNEL_STATE_LABELS: LabelMap = {
  approval_required: 'Approval required',
  blocked: 'Blocked',
  delivered: 'Delivered',
  pending: 'Pending',
  redacted: 'Redacted',
};

const REDACTION_LABELS: LabelMap = {
  applied: 'Applied',
  false: 'Not applied',
  none: 'Not applied',
  not_applied: 'Not applied',
  redacted: 'Applied',
  true: 'Applied',
};

const BLOCKER_LABELS: LabelMap = {
  approval_required: 'Approval required',
  channel_disabled: 'Channel disabled',
  channel_unhealthy: 'Channel unhealthy',
  channel_unknown: 'Unknown channel',
  config_missing: 'Configuration missing',
  none: 'No blocker',
  remote_intent_denied: 'Remote intent denied',
  remote_intent_missing: 'Remote intent missing',
};

function normalizeToken(value: unknown): string {
  return String(value ?? '').trim().toLowerCase();
}

function humanizeToken(value: unknown): string {
  const text = String(value ?? '').trim();
  if (!text) {
    return '';
  }
  return text
    .replace(/[_-]+/g, ' ')
    .replace(/\s+/g, ' ')
    .replace(/^\w/, (letter) => letter.toUpperCase());
}

export function formatDisplayLabel(value: unknown, labels: LabelMap, context: string): string {
  const token = normalizeToken(value);
  if (!token) {
    return `Unknown ${context}`;
  }
  return labels[token] ?? `Unknown ${context}: ${humanizeToken(value)}`;
}

export function formatChannelState(state: unknown): string {
  return formatDisplayLabel(state, CHANNEL_STATE_LABELS, 'channel state');
}

export function formatRedactionState(redaction: unknown): string {
  return formatDisplayLabel(redaction, REDACTION_LABELS, 'redaction state');
}

export function formatBlockedReason(reason: unknown): string {
  if (reason === null || reason === undefined || String(reason).trim() === '') {
    return BLOCKER_LABELS.none;
  }
  return formatDisplayLabel(reason, BLOCKER_LABELS, 'blocker');
}
