import { sanitizeTrace } from './traceRedaction.js';

export class GuardrailState {
  check = $state({ status: 'unknown', message: 'Safety check has not run.' });
  auth = $state({ permitted: false, status: 'unknown' });

  canProceed = $derived(this.check.status === 'ok' && this.auth.permitted === true);

  setCheck(result = {}) {
    this.check = {
      status: result.status === 'ok' ? 'ok' : 'blocked',
      message: sanitizeTrace(result.message ?? result.error?.message ?? 'Action is blocked until safety checks pass.'),
    };
  }

  setAuth(result = {}) {
    this.auth = {
      permitted: result.permitted === true,
      status: result.permitted === true ? 'authorized' : 'unauthorized',
    };
  }
}

export function sanitizedErrorEnvelope(error = {}) {
  const payload = error.error ?? error;
  return {
    code: payload.code ?? 'VETINARI_ERR_UI_GUARDRAIL',
    message: sanitizeTrace(payload.message ?? 'The guarded action failed.'),
    next_steps: payload.next_steps ?? 'Review the guardrail result and retry after the blocker is cleared.',
    doc_link: payload.doc_link ?? 'docs/troubleshooting.md#workbench-guardrail-blocked',
  };
}
