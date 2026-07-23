import { workbenchKernelRequest } from '$lib/api.js';

const API_ROOT = '/api/v1/workbench/launcher';

function tauriInvoke() {
  return globalThis.__TAURI__?.core?.invoke ?? globalThis.__TAURI_INTERNALS__?.invoke ?? null;
}

async function jsonFetch(url, options = {}) {
  return workbenchKernelRequest(url, options);
}

class LauncherStore {
  status = $state(null);
  isLoading = $state(false);
  lastError = $state(null);
  lastDecision = $state(null);
  doctorReport = $state(null);
  supportBundleResult = $state(null);
  healthStream = $state(null);

  canOpenUi = $derived(this.status?.is_ready === true);
  hasBlockers = $derived((this.status?.gates ?? []).some((gate) => !gate.passed));

  async loadStatus() {
    this.isLoading = true;
    this.lastError = null;
    try {
      this.status = await jsonFetch(`${API_ROOT}/status`);
      return this.status;
    } catch (error) {
      this.lastError = error.message ?? String(error);
      throw error;
    } finally {
      this.isLoading = false;
    }
  }

  async dispatchAction(action, opts = {}) {
    this.lastError = null;
    const payload = { action, ...opts };
    const invoke = tauriInvoke();
    try {
      const result = invoke
        ? await invoke('workbench_lifecycle_command', { payload })
        : await jsonFetch(`${API_ROOT}/action`, {
            method: 'POST',
            body: JSON.stringify(payload),
          });
      this.lastDecision = result.decision;
      this.status = result.status;
      return result;
    } catch (error) {
      this.lastError = error.message ?? String(error);
      throw error;
    }
  }

  async runDoctor() {
    this.lastError = null;
    try {
      this.doctorReport = await jsonFetch(`${API_ROOT}/doctor`, { method: 'POST', body: '{}' });
      return this.doctorReport;
    } catch (error) {
      this.lastError = error.message ?? String(error);
      throw error;
    }
  }

  async requestSupportBundle(spec) {
    this.lastError = null;
    try {
      this.supportBundleResult = await jsonFetch(`${API_ROOT}/support-bundle`, {
        method: 'POST',
        body: JSON.stringify(spec),
      });
      return this.supportBundleResult;
    } catch (error) {
      this.lastError = error.message ?? String(error);
      throw error;
    }
  }

  subscribeToHealthStream() {
    this.healthStream?.close?.();
    const source = new EventSource(`${API_ROOT}/health-stream`);
    source.onmessage = (event) => {
      this.status = JSON.parse(event.data);
    };
    source.onerror = () => {
      this.lastError = 'Health stream disconnected.';
    };
    this.healthStream = source;
    return () => {
      source.close();
      if (this.healthStream === source) {
        this.healthStream = null;
      }
    };
  }
}

export const LIFECYCLE_ACTIONS = [
  'open',
  'close_window',
  'keep_in_background',
  'stop',
  'restart',
  'quit_completely',
  'force_quit',
  'crash_recover',
];

export const launcherStore = new LauncherStore();
export { LauncherStore };
