import { ContractViolationError, normalizeApiError } from './api_contract.js';

export function createFailClosedAsyncStore({ label, loader, validate, initialValue = null } = {}) {
  const state = {
    label: label || 'async resource',
    status: 'idle',
    value: initialValue,
    error: null,
    async refresh(...args) {
      if (typeof loader !== 'function') {
        state.status = 'blocked';
        state.error = new ContractViolationError(`${state.label} loader is unavailable`, { label: state.label });
        throw state.error;
      }
      state.status = 'loading';
      state.error = null;
      try {
        const raw = await loader(...args);
        const value = typeof validate === 'function' ? validate(raw) : raw;
        state.value = value;
        state.status = 'ready';
        return value;
      } catch (error) {
        state.status = 'blocked';
        state.error = normalizeApiError(error, `${state.label} failed`);
        throw state.error;
      }
    },
    reset(value = initialValue) {
      state.value = value;
      state.error = null;
      state.status = 'idle';
    },
  };
  return state;
}
