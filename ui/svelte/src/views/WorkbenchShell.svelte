<script>
  import { appState } from '$lib/stores/app.svelte.js';
  import { showToast } from '$lib/stores/toast.svelte.js';
  import { getWorkbenchShellSnapshot } from '$lib/api.js';
  import WorkbenchLoadingSkeleton from '$components/WorkbenchLoadingSkeleton.svelte';
  import { ShellCommandSurface } from '$lib/components/command';
  import { ShellObjectNav } from '$lib/components/navigation';
  import {
    ObjectDrawer,
    QueuePanel,
    RiskControlBar,
    SplitComparisonPanel,
    TimelinePanel,
  } from '$lib/components/workbench/shell';
  import HelpPopover from '$lib/components/help/HelpPopover.svelte';
  import HelpTooltip from '$lib/components/help/HelpTooltip.svelte';

  let { projectId = new URLSearchParams(window.location.search).get('project_id') ?? 'default' } = $props();

  let snapshot = $state(null);
  let selectedObject = $state(null);
  let loading = $state(true);
  let error = $state(null);

  let selectedObjectId = $derived(selectedObject?.object_id ?? snapshot?.objects?.[0]?.object_id ?? null);
  let selectedWhy = $derived(selectedObject?.why ?? snapshot?.objects?.[0]?.why ?? snapshot?.degraded_reason ?? '');

  function navigate(view) {
    appState.currentView = view;
  }

  function runCommand(command) {
    if (!command.enabled) {
      showToast(command.blocked_reason || command.why, 'warning');
      return;
    }
    if (command.view && command.view !== 'workbench-shell') {
      appState.currentView = command.view;
    }
    showToast(command.why, command.requires_approval ? 'warning' : 'info');
  }

  async function loadShell() {
    loading = true;
    error = null;
    try {
      const data = await getWorkbenchShellSnapshot(projectId);
      snapshot = data;
      selectedObject = data.objects?.[0] ?? null;
    } catch (err) {
      error = err.message ?? String(err);
      showToast(`Workbench shell load failed: ${error}`, 'error');
    } finally {
      loading = false;
    }
  }

  function handleKeydown(event) {
    if (!snapshot || event.ctrlKey || event.metaKey || event.altKey) return;
    const key = event.key.toLowerCase();
    const command = snapshot.commands.find((row) => row.shortcut.toLowerCase() === key);
    if (!command) return;
    event.preventDefault();
    runCommand(command);
  }

  $effect(() => {
    void loadShell();
  });
</script>

<svelte:window onkeydown={handleKeydown} />

<main class="workbench-shell" aria-label="Workbench Shell" data-view="workbench-shell" data-testid="workbench-shell">
  <header class="shell-header">
    <div>
      <h1>Workbench Shell</h1>
      <p>Primary workbench surface for project {projectId} — object navigation, commands, queue, and timeline.</p>
      <HelpPopover
        title="Workbench shell"
        body="The shell is the primary operator surface. Object nav: select a project object (plan, run, asset, eval) to view its detail, split comparison, and timeline. Risk control bar: shows the current risk posture; a degraded banner appears when any risk signal is elevated. Commands: the command surface lists actions available for the selected object; disabled commands show the reason they are unavailable. Queue panel: shows pending and active run-kernel tasks; click a queue item to inspect it. Keyboard shortcuts: press ? to open the shortcut reference. Degraded banner: appears when the shell snapshot returns a degraded_reason — resolve the underlying signal to clear it."
        severity="info"
      />
    </div>
    <button type="button" class="refresh-button" aria-label="Refresh workbench shell" onclick={loadShell}>
      <i class="fas fa-rotate-right"></i>
    </button>
  </header>

  {#if loading}
    <WorkbenchLoadingSkeleton />
  {:else if error}
    <section class="state error" role="alert">{error}</section>
  {:else if snapshot}
    <ShellObjectNav items={snapshot.navigation} onNavigate={navigate} />

    {#if snapshot.degraded}
      <aside class="degraded-banner" role="alert" data-testid="workbench-shell-degraded">
        {snapshot.degraded_reason}
      </aside>
    {/if}

    <RiskControlBar risk={snapshot.risk_control} />

    <section class="shell-grid" aria-label="Workbench shell workspace">
      <ObjectDrawer
        objects={snapshot.objects}
        {selectedObjectId}
        runtimeUx={snapshot.runtime_ux}
        onSelect={(object) => {
          selectedObject = object;
        }}
      />

      <section class="shell-main-pane" aria-label="Selected object">
        <div class="selected-object" data-testid="workbench-selected-object">
          <span>{selectedObject?.object_kind || 'project'}</span>
          <h2>{selectedObject?.title || 'No object selected'}</h2>
          <p>{selectedWhy}</p>
        </div>
        <SplitComparisonPanel comparison={snapshot.split_comparison} objects={snapshot.objects} />
        <TimelinePanel events={snapshot.timeline} />
      </section>

      <aside class="shell-side-pane">
        <ShellCommandSurface commands={snapshot.commands} onRun={runCommand} />
        <QueuePanel queue={snapshot.queue} />
      </aside>
    </section>
  {/if}
</main>

<style>
  .workbench-shell {
    display: grid;
    gap: 12px;
    max-width: 1480px;
    padding: 16px;
    color: var(--text-primary, #e5e7eb);
  }

  .shell-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 12px;
  }

  h1,
  h2,
  p {
    margin: 0;
  }

  h1 {
    font-size: 1.25rem;
  }

  .shell-header p {
    color: var(--text-muted, #94a3b8);
    font-family: var(--font-mono);
    font-size: 0.82rem;
    margin-top: 3px;
  }

  .refresh-button {
    width: 34px;
    height: 34px;
    border: 1px solid var(--border-default, #334155);
    border-radius: 6px;
    background: var(--surface-elevated, #111827);
    color: var(--text-primary, #e5e7eb);
    cursor: pointer;
  }

  .refresh-button:focus-visible {
    outline: 2px solid #38bdf8;
    outline-offset: 2px;
  }

  .state,
  .degraded-banner,
  .selected-object {
    border: 1px solid var(--border-default, #334155);
    border-radius: 8px;
    background: var(--surface-elevated, #111827);
    padding: 14px;
  }

  .state {
    color: var(--text-muted, #94a3b8);
  }

  .state.error {
    color: #fca5a5;
  }

  .degraded-banner {
    border-color: #f59e0b;
    background: rgba(146, 64, 14, 0.18);
    color: #fcd34d;
  }

  .shell-grid {
    display: grid;
    grid-template-columns: minmax(260px, 0.74fr) minmax(420px, 1.5fr) minmax(280px, 0.86fr);
    gap: 12px;
    align-items: start;
  }

  .shell-main-pane,
  .shell-side-pane {
    display: grid;
    gap: 12px;
    min-width: 0;
  }

  .selected-object {
    display: grid;
    gap: 8px;
  }

  .selected-object span {
    color: var(--text-muted, #94a3b8);
    font-size: 0.76rem;
    text-transform: uppercase;
  }

  .selected-object h2 {
    font-size: 1rem;
    overflow-wrap: anywhere;
  }

  .selected-object p {
    color: var(--text-muted, #94a3b8);
    font-size: 0.84rem;
  }

  @media (max-width: 1180px) {
    .shell-grid {
      grid-template-columns: 1fr;
    }
  }
</style>
