<script>
  import { onMount } from 'svelte';
  import {
    CrashRecoveryBanner,
    HealthGateList,
    LifecycleActionMenu,
    LifecycleStatusCard,
    SupportBundleDialog,
    launcherStore,
  } from '$lib/components/workbench/lifecycle';
  import HelpPopover from '$lib/components/help/HelpPopover.svelte';
  import HelpTooltip from '$lib/components/help/HelpTooltip.svelte';

  let mode = $state('');
  let trayEnabled = $state(null);
  let backendPort = $state(null);

  function launcherConfig() {
    return launcherStore.status?.config ?? launcherStore.status?.launcher_config ?? {};
  }

  function resolvedLauncherSettings() {
    const config = launcherConfig();
    const resolvedPort = backendPort ?? config.backend_port ?? null;
    return {
      mode: mode || config.mode || '',
      tray_enabled: trayEnabled ?? config.tray_enabled ?? false,
      backend_port: resolvedPort == null || resolvedPort === '' ? null : Number(resolvedPort),
    };
  }

  function launcherSettingsReady() {
    const settings = resolvedLauncherSettings();
    return Boolean(
      settings.mode &&
      Number.isInteger(settings.backend_port) &&
      settings.backend_port >= 1 &&
      settings.backend_port <= 65535
    );
  }

  async function runAction(action) {
    if (!launcherSettingsReady()) {
      throw new Error('Launcher action requires runtime mode and backend port from config or explicit input.');
    }
    await launcherStore.dispatchAction(action, resolvedLauncherSettings());
  }

  async function createBundle(spec) {
    await launcherStore.requestSupportBundle(spec);
  }

  onMount(() => {
    void launcherStore.loadStatus();
    const cleanup = launcherStore.subscribeToHealthStream();
    return cleanup;
  });
</script>

<main class="launcher-settings" data-view="launcher-settings" data-testid="launcher-settings">
  <header>
    <div>
      <h1>Launcher Settings</h1>
      <p>Desktop shell, browser path, background mode, and lifecycle controls.</p>
      <HelpPopover
        title="Launcher settings"
        body="First-run setup: the launcher must complete health gate checks before the workbench becomes fully operational — the health gate list shows which gates remain open. Backend health status: the lifecycle status card reflects the last health probe result; a degraded or error state means the backend process is running but one or more subsystems are unhealthy. Shutdown semantics: graceful shutdown allows in-flight requests to complete before the process exits; force shutdown terminates immediately (may lose unsaved state). Restart: restarts the backend process without reloading the desktop shell — useful after config changes that require a process restart but not a full shell reload. Mode: desktop_default opens the workbench in a desktop window; browser_open uses the default browser; background_only starts the backend without a UI window."
        severity="info"
      />
    </div>
    <button type="button" aria-label="Refresh launcher status" onclick={() => launcherStore.loadStatus()}>
      <i class="fas fa-rotate-right" aria-hidden="true"></i>
    </button>
  </header>

  <CrashRecoveryBanner status={launcherStore.status} />
  <LifecycleStatusCard status={launcherStore.status} isLoading={launcherStore.isLoading} />

  <section class="settings-grid" aria-label="Launcher settings parity">
    <label>
      Default mode
      <select bind:value={mode} data-testid="launcher-mode-select">
        <option value="">Use configured mode</option>
        <option value="desktop_default">Desktop</option>
        <option value="browser_open">Browser</option>
        <option value="background_only">Background</option>
      </select>
    </label>
    <label class="checkbox">
      <input type="checkbox" bind:checked={trayEnabled} />
      Tray enabled
    </label>
    <label>
      Backend port
      <input type="number" min="1" max="65535" placeholder={launcherConfig().backend_port == null ? 'Configured port' : String(launcherConfig().backend_port)} bind:value={backendPort} data-testid="launcher-backend-port" />
    </label>
  </section>

  <LifecycleActionMenu disabled={launcherStore.isLoading || !launcherSettingsReady()} onAction={runAction} />
  <HealthGateList gates={launcherStore.status?.gates ?? []} />
  <SupportBundleDialog onSubmit={createBundle} result={launcherStore.supportBundleResult} error={launcherStore.lastError} />
</main>

<style>
  .launcher-settings {
    display: grid;
    gap: 14px;
    max-width: 1180px;
    padding: 16px;
    color: var(--text-primary, #e5e7eb);
  }

  header,
  .settings-grid {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 12px;
  }

  h1,
  p {
    margin: 0;
  }

  h1 {
    font-size: 1.25rem;
  }

  p {
    color: var(--text-muted, #94a3b8);
  }

  button,
  input,
  select {
    min-height: 34px;
    border: 1px solid var(--border-default, #334155);
    border-radius: 6px;
    background: var(--surface-elevated, #111827);
    color: var(--text-primary, #e5e7eb);
  }

  .settings-grid {
    align-items: center;
    border: 1px solid var(--border-default, #334155);
    border-radius: 8px;
    padding: 12px;
  }

  label {
    display: grid;
    gap: 5px;
    color: var(--text-muted, #94a3b8);
    font-size: 0.84rem;
  }

  .checkbox {
    display: flex;
    align-items: center;
  }

  @media (max-width: 760px) {
    .launcher-settings {
      max-width: none;
      padding: 12px;
    }

    header,
    .settings-grid {
      align-items: stretch;
      flex-direction: column;
    }

    header button {
      align-self: flex-start;
    }

    .settings-grid label,
    .settings-grid input,
    .settings-grid select {
      width: 100%;
    }

    .checkbox {
      align-items: center;
    }
  }
</style>
