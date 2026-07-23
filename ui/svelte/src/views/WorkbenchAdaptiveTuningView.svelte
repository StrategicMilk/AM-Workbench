<script>
  import {
    decideAdaptiveTuningHypothesis,
    forgetAdaptiveTuningHypothesis,
    getAdaptiveTuningSnapshot,
    previewAdaptiveTuningProposal,
    revokeAdaptiveTuningHypothesis,
  } from '$lib/api.js';
  import { AdaptiveTuningPanel } from '$lib/components/workbench/adaptive-tuning';
  import HelpPopover from '$lib/components/help/HelpPopover.svelte';
  import HelpTooltip from '$lib/components/help/HelpTooltip.svelte';

  let { projectId = 'default' } = $props();
  let snapshot = $state(defaultSnapshot('default'));
  let error = $state('');

  function defaultSnapshot(selectedProject) {
    return { schema_version: 1, project_id: selectedProject, hypotheses: [], controls: [] };
  }

  async function loadSnapshot(selectedProject) {
    try {
      snapshot = await getAdaptiveTuningSnapshot(selectedProject);
      error = '';
      return true;
    } catch (err) {
      snapshot = defaultSnapshot(selectedProject);
      error = `AdaptiveTuningView refreshSnapshot failed: ${err.message}`;
      return false;
    }
  }

  $effect(() => {
    const selectedProject = projectId || 'default';
    loadSnapshot(selectedProject);
  });

  async function handleAction(event) {
    const selectedProject = projectId || 'default';
    const payload = {
      action: event.action,
      actor_ref: 'ui',
      decided_at_utc: new Date().toISOString(),
    };
    try {
      if (event.action === 'forget') {
        await forgetAdaptiveTuningHypothesis(selectedProject, event.hypothesisId, payload);
      } else if (event.action === 'revoke' || event.action === 'rollback') {
        await revokeAdaptiveTuningHypothesis(selectedProject, event.hypothesisId, payload);
      } else if (event.action === 'preview' || event.action === 'edit') {
        await previewAdaptiveTuningProposal({ project_id: selectedProject, hypothesis_id: event.hypothesisId });
      } else {
        await decideAdaptiveTuningHypothesis(selectedProject, event.hypothesisId, payload);
      }
      const refreshed = await loadSnapshot(selectedProject);
      if (refreshed) {
        error = '';
      }
    } catch (err) {
      error = `AdaptiveTuningView handleAction failed: ${err.message}`;
    }
  }
</script>

<section class="adaptive-tuning-view" aria-label="Adaptive tuning review">
  <header class="view-header">
    <div>
      <h1>Adaptive Tuning</h1>
      <p>Review friction hypotheses and approve or reject proposed tuning changes.</p>
      <HelpPopover
        title="Adaptive tuning"
        body="Hypotheses are generated when the workbench detects recurring friction patterns in agent runs. Each hypothesis proposes a configuration change to reduce that friction. Lifecycle: detected → proposed → awaiting_approval → promoted or rejected. Promotion gate: a hypothesis must pass a simulation probe before it can be promoted to active. Delete removes the hypothesis without promoting. Revoke rolls back a promoted hypothesis. Truth-vs-preference note: hypotheses reflect observed patterns, not operator preference — approving a hypothesis that contradicts your intended configuration may have unintended effects."
        severity="info"
      />
    </div>
  </header>
  {#if error}
    <p class="error" role="alert" aria-live="assertive">{error}</p>
  {/if}
  <AdaptiveTuningPanel {projectId} {snapshot} onAction={handleAction} />
</section>

<style>
  .adaptive-tuning-view {
    display: grid;
    gap: 12px;
  }
  .view-header { padding: 0 0 4px; }
  .view-header h1 { margin: 0 0 4px; font-size: 24px; }
  .view-header p { margin: 0; color: var(--text-muted); }

  .error {
    margin: 0;
    color: #fca5a5;
    padding: 12px 16px 0;
  }

  @media (max-width: 720px) {
    .adaptive-tuning-view {
      gap: 10px;
      overflow-x: hidden;
    }

    .view-header {
      padding: 0;
    }

    .view-header h1 {
      font-size: 20px;
    }

    .error {
      padding: 10px 0 0;
    }
  }
</style>
