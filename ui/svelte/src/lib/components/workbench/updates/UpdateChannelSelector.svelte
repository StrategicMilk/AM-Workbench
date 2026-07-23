<script>
  import { UpdateChannel } from '$lib/contracts';

  const DEFAULT_CHANNELS = ['stable', 'beta', 'canary'];

  let {
    channel = UpdateChannel.STABLE,
    channels = DEFAULT_CHANNELS,
    onChange = () => {},
    disabled = false,
  } = $props();

  let channelOptions = $derived(
    Array.from(new Set((Array.isArray(channels) ? channels : []).filter((item) => typeof item === 'string' && item.trim())))
  );
  let validChannel = $derived(channelOptions.includes(channel));

  function changeChannel(event) {
    const next = event.currentTarget.value;
    if (!channelOptions.includes(next)) return;
    onChange(next);
  }
</script>

<label class="channel-selector">
  <span>channel</span>
  <select value={validChannel ? channel : ''} disabled={disabled || channelOptions.length === 0} onchange={changeChannel} aria-invalid={!validChannel}>
    {#if !validChannel}
      <option value="">unavailable</option>
    {/if}
    {#each channelOptions as item}
      <option value={item}>{item}</option>
    {/each}
  </select>
</label>

<style>
  .channel-selector {
    display: flex;
    gap: 8px;
    align-items: center;
    min-width: 180px;
  }
  span {
    color: var(--text-muted);
    font-size: 12px;
    text-transform: uppercase;
  }
  select {
    min-height: 34px;
    border: 1px solid var(--border-default);
    border-radius: 6px;
    background: var(--surface-elevated, #111827);
    color: var(--text-primary);
    padding: 6px 8px;
  }
</style>
