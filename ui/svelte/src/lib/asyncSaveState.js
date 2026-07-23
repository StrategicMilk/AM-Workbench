import { requireEvidence } from '$lib/evidence/evidenceGuard.js';

export const SAVE_LIFECYCLE_MESSAGES = Object.freeze({
  missingResponse: 'No response was returned.',
  pending: 'Save accepted; waiting for backend confirmation.',
  saved: 'Save confirmed.',
  failed: 'Save failed before confirmation.',
  invalidEvidence: 'Save response contained invalid evidence refs.',
});

function saveEvidenceRefs(response) {
  return [
    ...(Array.isArray(response.evidence_refs) ? response.evidence_refs : []),
    response.evidence_ref,
    response.receipt_id,
    response.persisted_state_ref,
  ].filter(Boolean);
}

export function resolveSaveLifecycle(response, messages = SAVE_LIFECYCLE_MESSAGES) {
  if (!response) {
    return { status: 'failed', message: messages.missingResponse };
  }
  try {
    requireEvidence(saveEvidenceRefs(response), 'async-save-response');
  } catch (error) {
    return {
      status: 'failed',
      message: messages.invalidEvidence,
      error,
    };
  }
  if (response.status === 202) {
    return { status: 'pending', message: messages.pending };
  }
  if (response.status >= 200 && response.status < 300) {
    return { status: 'saved', message: messages.saved };
  }
  return { status: 'failed', message: messages.failed };
}
