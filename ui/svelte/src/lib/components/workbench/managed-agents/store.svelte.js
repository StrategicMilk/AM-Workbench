import { workbenchKernelRequest } from '$lib/api.js';

const API_ROOT = '/api/workbench/managed-agents';

async function request(path, options = {}) {
  return workbenchKernelRequest(`${API_ROOT}${path}`, options);
}

function post(path, body = {}) {
  return request(path, { method: 'POST', body: JSON.stringify(body) });
}

function createManagedAgentsStore() {
  let snapshot = $state(null);
  let loading = $state(false);
  let error = $state(null);
  let lastDecision = $state(null);

  async function refresh() {
    loading = true;
    error = null;
    try {
      snapshot = await request('/snapshot');
    } catch (err) {
      error = err.message;
    } finally {
      loading = false;
    }
  }

  async function pause(agentId, reason = 'User paused from managed-agent workspace') {
    lastDecision = await post(`/${encodeURIComponent(agentId)}/pause`, { reason, actor: 'user' });
    await refresh();
  }

  async function retire(agentId, reason = 'User retired from managed-agent workspace') {
    lastDecision = await post(`/${encodeURIComponent(agentId)}/retire`, { reason, actor: 'user' });
    await refresh();
  }

  return {
    get snapshot() {
      return snapshot;
    },
    get loading() {
      return loading;
    },
    get error() {
      return error;
    },
    get lastDecision() {
      return lastDecision;
    },
    refresh,
    pause,
    retire,
  };
}

export const managedAgentsStore = createManagedAgentsStore();
