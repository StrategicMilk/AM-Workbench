<script>
  let { config = null, selectedChannelId = '', onSelect = () => {} } = $props();
  let channels = $derived(config?.channels ?? []);

  function stateClass(channel) {
    if (channel.lifecycle_state !== 'enabled') return 'blocked';
    if (channel.health_state !== 'healthy') return 'degraded';
    return 'ready';
  }

  function channelLabel(channel) {
    const selected = selectedChannelId === channel.channel_id ? 'selected' : 'not selected';
    const capabilities = Array.isArray(channel.capabilities) && channel.capabilities.length ? channel.capabilities.join(', ') : 'no capabilities listed';
    return `${channel.display_name ?? channel.channel_id}, ${channel.channel_type ?? 'unknown'}, ${channel.lifecycle_state ?? 'unknown'} lifecycle, ${channel.health_state ?? 'unknown'} health, ${selected}, capabilities: ${capabilities}`;
  }
</script>

<section class="hub-panel" aria-label="Channel Hub channels">
  <header><h2>Channels</h2><span role="status" aria-label={`${channels.length} channels registered`}>{channels.length} registered</span></header>
  <div class="channel-grid">
    {#if channels.length === 0}
      <p class="empty-state" role="status">No channels registered.</p>
    {/if}
    {#each channels as channel}
      <button
        type="button"
        class="channel-row"
        class:active={selectedChannelId === channel.channel_id}
        data-state={stateClass(channel)}
        aria-pressed={selectedChannelId === channel.channel_id}
        aria-label={channelLabel(channel)}
        onclick={() => onSelect(channel.channel_id)}
      >
        <span class="channel-title">{channel.display_name ?? channel.channel_id}</span>
        <span>{channel.channel_type ?? 'unknown'}</span>
        <span>{channel.lifecycle_state ?? 'unknown'} / {channel.health_state ?? 'unknown'}</span>
        <span class="capabilities">{Array.isArray(channel.capabilities) && channel.capabilities.length ? channel.capabilities.join(', ') : 'no capabilities listed'}</span>
      </button>
    {/each}
  </div>
</section>

<style>
  .hub-panel { display: flex; flex-direction: column; gap: 12px; }
  header { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
  h2 { margin: 0; font-size: 18px; }
  header span { color: var(--text-muted); font-size: 13px; }
  .channel-grid { display: grid; gap: 8px; }
  .channel-row { display: grid; grid-template-columns: minmax(160px, 1.2fr) minmax(90px, .7fr) minmax(140px, .8fr) minmax(180px, 1.4fr); align-items: center; gap: 12px; width: 100%; min-height: 52px; padding: 10px 12px; border: 1px solid var(--border-default); border-radius: 8px; background: var(--surface-elevated); color: var(--text-primary); text-align: left; }
  .channel-row.active { border-color: var(--color-primary); }
  .channel-row[data-state="blocked"] { border-left: 4px solid #d24b4b; }
  .channel-row[data-state="degraded"] { border-left: 4px solid #d69b2d; }
  .channel-row[data-state="ready"] { border-left: 4px solid #3c9f72; }
  .channel-title { font-weight: 700; }
  .capabilities { color: var(--text-muted); overflow-wrap: anywhere; }
  .empty-state { margin: 0; color: var(--text-muted); font-size: 13px; }
  @media (max-width: 760px) { .channel-row { grid-template-columns: 1fr; } }
</style>
