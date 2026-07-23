import { fetchWorkbenchStatusSnapshot, runWorkbenchStatusAction } from '$lib/api.js';

export function createWorkbenchStatusStore(projectId = 'default') {
  let snapshot = $state(null);
  let loading = $state(false);
  let error = $state('');
  let actionResult = $state(null);

  async function refresh() {
    loading = true;
    error = '';
    try {
      snapshot = await fetchWorkbenchStatusSnapshot(projectId);
    } catch (err) {
      error = err instanceof Error ? err.message : String(err);
    } finally {
      loading = false;
    }
  }

  async function runAction(payload) {
    actionResult = await runWorkbenchStatusAction({ project_id: projectId, ...payload });
    return actionResult;
  }

  return {
    get snapshot() { return snapshot; },
    get loading() { return loading; },
    get error() { return error; },
    get actionResult() { return actionResult; },
    refresh,
    runAction,
  };
}
