<script>
  let {
    channelId = 'desktop',
    loading = false,
    deliveryResult = null,
    commandResult = null,
    initialPayload = {},
    initialMedia = {},
    actorId = 'workbench-channel-preview',
    actionType = 'channel_delivery',
    onDeliver = () => {},
    onCommand = () => {}
  } = $props();
  let payload = $derived({
    summary: 'Preview Channel Hub delivery',
    message: 'Channel-safe update',
    credential_ref: '',
    ...initialPayload,
    redaction_required: true,
  });
  let media = $derived({
    media_id: 'preview-attachment',
    media_type: 'text/plain',
    payload: 'safe text',
    metadata: { id: 'preview-attachment', name: 'preview.txt', redaction_required: true },
    ...initialMedia,
    metadata: stripSecretFields({ id: 'preview-attachment', name: 'preview.txt', redaction_required: true, ...(initialMedia.metadata ?? {}) }),
  });
  let previewResult = $derived(commandResult ?? deliveryResult);
  const previewSensitiveKeys = ['api_token', 'token', 'secret', 'local_path'];
  const sensitiveKeys = new Set([...previewSensitiveKeys, 'api-token', 'apikey', 'api_key', 'password']);

  function stripSecretFields(value) {
    if (!value || typeof value !== 'object') return value;
    if (Array.isArray(value)) return value.map(stripSecretFields);
    return Object.fromEntries(
      Object.entries(value)
        .filter(([key]) => !sensitiveKeys.has(String(key).toLowerCase()))
        .map(([key, item]) => [key, stripSecretFields(item)])
    );
  }

  function requestPayload() {
    return {
      channel_id: channelId,
      run_id: 'channel-hub-preview',
      actor_id: actorId,
      action_id: 'preview-delivery',
      action_type: actionType,
      action_fingerprint: `channel:${channelId}:preview-delivery`,
      summary: payload.summary,
      payload: { ...stripSecretFields(payload), redaction_required: true },
      media: [{ ...stripSecretFields(media), metadata: stripSecretFields(media.metadata), redaction_required: true }],
    };
  }
</script>

<section class="delivery-drawer" aria-label="Channel delivery preview">
  <header><h2>Delivery Preview</h2><span>{channelId}</span></header>
  <div class="actions">
    <button type="button" onclick={() => onDeliver(requestPayload())} disabled={loading}>Preview</button>
    <button type="button" onclick={() => onCommand(requestPayload())} disabled={loading}>Route Command</button>
  </div>
  {#if previewResult}
    <section class="result" data-state={previewResult.state} role="status" aria-live="polite">
      <div><span>State</span><strong>{previewResult.state}</strong></div>
      <div><span>Redaction</span><strong>{previewResult.redaction_applied}</strong></div>
      <div><span>Blocked</span><strong>{previewResult.blocked_reason ?? 'none'}</strong></div>
    </section>
    <pre>{JSON.stringify(previewResult.envelope, null, 2)}</pre>
  {:else}
    <p>No delivery preview loaded.</p>
  {/if}
</section>

<style>
  .delivery-drawer { display: flex; flex-direction: column; gap: 12px; }
  header, .actions, .result { display: flex; align-items: center; gap: 10px; }
  header { justify-content: space-between; }
  h2, p { margin: 0; }
  header span, p { color: var(--text-muted); }
  button { border: 1px solid var(--border-default); border-radius: 8px; background: var(--surface-elevated); color: var(--text-primary); padding: 8px 12px; }
  .result { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .result div { border: 1px solid var(--border-default); border-radius: 8px; padding: 10px; background: var(--surface-elevated); }
  .result span { display: block; color: var(--text-muted); font-size: 12px; }
  pre { max-height: 260px; overflow: auto; border: 1px solid var(--border-default); border-radius: 8px; padding: 12px; background: var(--surface-elevated); color: var(--text-primary); }
  @media (max-width: 760px) { .result { grid-template-columns: 1fr; } }
</style>
