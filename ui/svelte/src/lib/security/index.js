import { EvidenceRequiredError, requireEvidence } from '$lib/evidence/evidenceGuard.js';

const SECRET_KEY_PATTERN = /(api[-_]?key|api[-_]?token|bearer|credential|password|secret|token)/i;
const LOCAL_PATH_KEY_PATTERN = /(local[-_]?path|manifest[-_]?path|bundle[-_]?path|filesystem|absolute[-_]?path)/i;
const WINDOWS_PATH_PATTERN = /(?:[A-Za-z]:[\\/]|\\\\[^\\/]+[\\/][^\\/]+)/;
const FILE_URI_PATTERN = /^file:/i;
const TRUSTED_STATUS = new Set(['allowed', 'available', 'current', 'ok', 'passing', 'ready', 'signed', 'trusted', 'verified']);
const BLOCKED_STATUS = new Set(['blocked', 'denied', 'failed', 'missing', 'rejected', 'revoked', 'stale', 'unavailable', 'unknown']);

export function evidenceRefsFrom(record, extraKeys = []) {
  const keys = [
    'evidence_refs',
    'provenance_refs',
    'authority_refs',
    'integrity_refs',
    'signature_refs',
    'source_ref',
    'source_uri',
    'source_url',
    'digest',
    'sha256',
    'signature_ref',
    'manifest_ref',
    ...extraKeys,
  ];
  const refs = [];
  for (const key of keys) {
    const value = record?.[key];
    if (Array.isArray(value)) {
      refs.push(...value);
    } else if (value) {
      refs.push(value);
    }
  }
  return refs.filter(Boolean);
}

export function redactSupplyChainValue(value, key = '') {
  if (value == null) return value;
  if (SECRET_KEY_PATTERN.test(String(key))) return '<redacted-secret>';
  if (LOCAL_PATH_KEY_PATTERN.test(String(key))) return '<redacted-local-path>';
  if (typeof value !== 'string') return value;
  if (FILE_URI_PATTERN.test(value) || WINDOWS_PATH_PATTERN.test(value)) return '<redacted-local-path>';
  if (/Bearer\s+[A-Za-z0-9._~-]+/i.test(value)) return value.replace(/Bearer\s+[A-Za-z0-9._~-]+/ig, 'Bearer <redacted>');
  return value;
}

export function redactSupplyChainPayload(value, key = '') {
  const redacted = redactSupplyChainValue(value, key);
  if (redacted !== value || redacted == null || typeof redacted !== 'object') {
    return redacted;
  }
  if (Array.isArray(value)) {
    return value.map((item) => redactSupplyChainPayload(item));
  }
  return Object.fromEntries(
    Object.entries(value).map(([entryKey, entryValue]) => [
      entryKey,
      redactSupplyChainPayload(entryValue, entryKey),
    ]),
  );
}

export function provenanceDecision(input = {}, context = 'supply-chain') {
  const refs = evidenceRefsFrom(input);
  const reasons = Array.isArray(input.reasons) ? input.reasons.filter(Boolean).map(String) : [];
  const status = String(input.status ?? input.trust_status ?? input.state ?? input.current_status ?? '').toLowerCase();
  const trustTier = String(input.trust_tier ?? input.trustTier ?? '').toLowerCase();
  const explicitAllowed = input.allowed === true || input.enablement?.allowed === true;
  const explicitDenied = input.allowed === false || input.enablement?.allowed === false;

  try {
    if (refs.length === 0) {
      throw new EvidenceRequiredError(context, ['missing-provenance-ref']);
    }
    requireEvidence(refs, context);
  } catch (error) {
    return {
      state: 'blocked',
      trusted: false,
      refs,
      reasons: [error.message ?? String(error), ...reasons],
    };
  }

  if (explicitDenied || BLOCKED_STATUS.has(status) || BLOCKED_STATUS.has(trustTier)) {
    return {
      state: 'blocked',
      trusted: false,
      refs,
      reasons: reasons.length > 0 ? reasons : [`${context} is not trusted`],
    };
  }

  if (explicitAllowed || TRUSTED_STATUS.has(status) || TRUSTED_STATUS.has(trustTier)) {
    return {
      state: 'trusted',
      trusted: true,
      refs,
      reasons,
    };
  }

  return {
    state: 'degraded',
    trusted: false,
    refs,
    reasons: reasons.length > 0 ? reasons : [`${context} trust state is undeclared`],
  };
}

export function requireTrustedProvenance(input = {}, context = 'supply-chain') {
  const decision = provenanceDecision(input, context);
  if (!decision.trusted) {
    throw new Error(decision.reasons.join('; '));
  }
  return decision;
}
