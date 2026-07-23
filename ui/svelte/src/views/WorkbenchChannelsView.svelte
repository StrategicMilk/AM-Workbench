<script>
  import {
    ChannelActivityTable,
    ChannelApprovalPanel,
    ChannelDeliveryDrawer,
    ChannelHubPanel,
    createWorkbenchChannelsStore,
  } from '$lib/components/workbench/channels';
  import HelpPopover from '$lib/components/help/HelpPopover.svelte';
  import HelpTooltip from '$lib/components/help/HelpTooltip.svelte';

  let { projectId = 'default' } = $props();
  const store = createWorkbenchChannelsStore();
  let selectedChannelId = $state('desktop');
  let selectedChannel = $derived((store.config?.channels ?? []).find((channel) => channel.channel_id === selectedChannelId));

  $effect(() => {
    void projectId;
    store.load();
  });
</script>

<section class="channels-view" aria-label="Workbench Channel Hub">
  <header class="view-header">
    <div>
      <h1>Channel Hub</h1>
      <p>Configure and manage delivery channels — desktop, browser, and background routing targets.</p>
      <HelpPopover
        title="Delivery channels"
        body="Each channel routes workbench output to a delivery target (desktop notification, browser tab, background queue). Lifecycle state shows whether the channel is active, paused, or retired. Health state reflects the last delivery probe result. Command authorization policy controls which commands may be routed through this channel. disabled or unhealthy channels will not receive deliveries until the underlying issue is resolved and the channel is re-activated. Approval routing: some channels require an approval gate before delivery proceeds."
        severity="info"
      />
    </div>
    <button type="button" onclick={() => store.load()} disabled={store.loading}>
      <i class="fas fa-rotate" aria-hidden="true"></i>
      <span>{store.loading ? 'Loading' : 'Refresh'}</span>
    </button>
  </header>

  {#if store.error}
    <div class="status-banner" role="alert">{store.error}</div>
  {/if}

  <section class="summary-strip">
    <div><span>Selected</span><strong>{selectedChannel?.display_name ?? selectedChannelId}</strong></div>
    <div><span>Lifecycle</span><strong>{selectedChannel?.lifecycle_state ?? 'unknown'}</strong></div>
    <div><span>Health</span><strong>{selectedChannel?.health_state ?? 'unknown'}</strong></div>
    <div><span>Command</span><strong>{selectedChannel?.command_authorization_policy ?? 'unknown'}</strong></div>
  </section>

  <div class="layout">
    <ChannelHubPanel config={store.config} selectedChannelId={selectedChannelId} onSelect={(id) => { selectedChannelId = id; }} />
    <ChannelDeliveryDrawer
      channelId={selectedChannelId}
      loading={store.loading}
      deliveryResult={store.deliveryResult}
      commandResult={store.commandResult}
      onDeliver={(payload) => store.previewDelivery(payload)}
      onCommand={(payload) => store.routeCommand(payload)}
    />
  </div>

  <div class="layout secondary">
    <ChannelApprovalPanel result={store.commandResult ?? store.deliveryResult} />
    <ChannelActivityTable items={store.activity} />
  </div>
</section>

<style>
  .channels-view { display: flex; flex-direction: column; gap: 18px; padding: 24px; color: var(--text-primary); }
  .view-header { display: flex; justify-content: space-between; align-items: center; gap: 16px; }
  h1, p { margin: 0; }
  .view-header p { color: var(--text-muted); margin-top: 4px; }
  .view-header button { border: 1px solid var(--border-default); border-radius: 8px; background: var(--surface-elevated); color: var(--text-primary); padding: 9px 12px; display: inline-flex; gap: 8px; align-items: center; }
  .status-banner { border: 1px solid var(--danger); border-radius: 8px; padding: 10px 12px; color: var(--danger); background: var(--danger-muted); }
  .summary-strip { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; }
  .summary-strip div { border: 1px solid var(--border-default); border-radius: 8px; padding: 12px; background: var(--surface-elevated); }
  .summary-strip span { display: block; color: var(--text-muted); font-size: 12px; }
  .summary-strip strong { display: block; margin-top: 4px; overflow-wrap: anywhere; }
  .layout { display: grid; grid-template-columns: minmax(420px, 1.1fr) minmax(320px, .9fr); gap: 18px; align-items: start; }
  .secondary { grid-template-columns: minmax(320px, .8fr) minmax(420px, 1.2fr); }
  @media (max-width: 980px) {
    .layout, .secondary, .summary-strip { grid-template-columns: 1fr; }
  }
</style>
