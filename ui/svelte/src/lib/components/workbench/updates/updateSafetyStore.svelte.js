import {
  checkWorkbenchUpdates,
  createWorkbenchUpdateRollbackPlan,
  createWorkbenchUpdateSupportBundle,
  fetchWorkbenchUpdateReadiness,
  skipWorkbenchUpdateVersion,
} from '$lib/api.js';
import { ReadinessState, UpdateChannel } from '$lib/contracts';

export function createUpdateSafetyStore({ projectId = 'default', channel = UpdateChannel.STABLE, currentVersion = '0.0.0-dev' } = {}) {
  let readiness = $state(null);
  let rollbackPlan = $state(null);
  let supportBundle = $state(null);
  let loading = $state(false);
  let error = $state('');
  let selectedChannel = $state(channel);
  let state = $derived(readiness?.state ?? ReadinessState.BLOCKED);
  let candidateVersion = $derived(readiness?.candidate_version ?? '');
  let noAutoInstall = $derived(readiness?.no_auto_install !== false);

  function activeProjectId() {
    return typeof projectId === 'function' ? projectId() : projectId;
  }

  function activeCurrentVersion() {
    return typeof currentVersion === 'function' ? currentVersion() : currentVersion;
  }

  async function refresh() {
    loading = true;
    error = '';
    try {
      readiness = await fetchWorkbenchUpdateReadiness(activeProjectId(), selectedChannel, activeCurrentVersion());
    } catch (err) {
      error = err instanceof Error ? err.message : String(err);
    } finally {
      loading = false;
    }
  }

  async function checkNow() {
    loading = true;
    error = '';
    try {
      readiness = await checkWorkbenchUpdates({
        project_id: activeProjectId(),
        channel: selectedChannel,
        current_version: activeCurrentVersion(),
        installed_release: true,
      });
    } catch (err) {
      error = err instanceof Error ? err.message : String(err);
    } finally {
      loading = false;
    }
  }

  async function skip(decision) {
    const result = await skipWorkbenchUpdateVersion({
      project_id: activeProjectId(),
      channel: selectedChannel,
      version: candidateVersion,
      approval_decision: decision,
      reason: 'user skipped update from Workbench status panel',
    });
    await refresh();
    return result;
  }

  async function requestRollbackPlan(decision = null) {
    rollbackPlan = await createWorkbenchUpdateRollbackPlan({
      project_id: activeProjectId(),
      channel: selectedChannel,
      current_version: activeCurrentVersion(),
      installed_release: true,
      confirm: Boolean(decision),
      approval_decision: decision,
    });
    return rollbackPlan;
  }

  async function createSupportBundle() {
    supportBundle = await createWorkbenchUpdateSupportBundle({
      project_id: activeProjectId(),
      channel: selectedChannel,
      current_version: activeCurrentVersion(),
      installed_release: true,
      recent_run_ids: ['workbench-update-panel'],
    });
    return supportBundle;
  }

  $effect(() => {
    void selectedChannel;
    void refresh();
  });

  return {
    get readiness() { return readiness; },
    get rollbackPlan() { return rollbackPlan; },
    get supportBundle() { return supportBundle; },
    get loading() { return loading; },
    get error() { return error; },
    get selectedChannel() { return selectedChannel; },
    set selectedChannel(value) { selectedChannel = value; },
    set error(value) { error = value; },
    get state() { return state; },
    get candidateVersion() { return candidateVersion; },
    get noAutoInstall() { return noAutoInstall; },
    refresh,
    checkNow,
    skip,
    requestRollbackPlan,
    createSupportBundle,
  };
}
