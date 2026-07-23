<script>
  import { normalizeWorkflowRuntimeSnapshot, RCG_0021_P05_WORKFLOW_RECEIPT } from './index.js';

  let { snapshot = {}, onSettings = async () => {} } = $props();
  let runtimeState = $derived(normalizeWorkflowRuntimeSnapshot(snapshot));
  let safeSnapshot = $derived(runtimeState.snapshot);
  let settings = $derived(safeSnapshot.runtime_settings);

  function resetSettings() {
    onSettings({
      max_parallel_steps: settings.max_parallel_steps ?? 1,
      safety_mode: settings.safety_mode ?? 'operator_confirmed',
      channel_preview_only: Boolean(settings.channel_preview_only),
      reset_source: safeSnapshot.active_graph_id ?? 'workflow-runtime-console',
    });
  }
</script>

<section class="console" aria-label="Workflow runtime console" data-rcg0021-p05-state={runtimeState.ok ? 'ready' : 'blocked'}>
  <header>
    <h2>Console</h2>
    <span>{safeSnapshot.saved_graph_count ?? 0} saved</span>
  </header>
  {#if !runtimeState.ok}
    <div class="status-banner" role="alert" aria-live="assertive">{runtimeState.issue}</div>
  {/if}
  <dl>
    <div>
      <dt>Active graph</dt>
      <dd>{safeSnapshot.active_graph_id ?? 'none'}</dd>
    </div>
    <div>
      <dt>Mode</dt>
      <dd>{settings.safety_mode ?? 'simulation_only'}</dd>
    </div>
    <div>
      <dt>Parallel steps</dt>
      <dd>{settings.max_parallel_steps ?? 2}</dd>
    </div>
    <div>
      <dt>Receipt</dt>
      <dd>{RCG_0021_P05_WORKFLOW_RECEIPT}</dd>
    </div>
  </dl>
  <button onclick={resetSettings}>
    Reset runtime settings
  </button>
</section>

<style>
  .console {
    border: 1px solid var(--border-default, #334155);
    border-radius: 8px;
    padding: 12px;
  }

  header,
  dl {
    display: grid;
    gap: 8px;
  }

  header {
    grid-template-columns: 1fr auto;
  }

  h2,
  dl,
  dd {
    margin: 0;
    letter-spacing: 0;
  }

  h2 {
    font-size: 16px;
  }

  dt,
  dd,
  span,
  button {
    font-size: 12px;
  }

  dt {
    color: var(--text-muted, #94a3b8);
  }

  button {
    margin-top: 12px;
    border: 1px solid var(--border-default, #334155);
    border-radius: 8px;
    background: transparent;
    color: inherit;
    padding: 8px 10px;
  }

  .status-banner {
    margin: 10px 0;
    border: 1px solid #d44d4d;
    border-radius: 8px;
    padding: 10px 12px;
    font-size: 12px;
  }
</style>
