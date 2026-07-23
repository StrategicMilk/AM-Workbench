<script>
  const { reason } = $props();

  const LABELS = {
    missing_capability: 'Missing capability',
    stage_not_serving: 'Not serving',
    deprecated: 'Deprecated',
    provider_disabled: 'Provider disabled',
    capability_snapshot_unavailable: 'Snapshot unavailable',
    blocked_by_policy: 'Blocked by policy',
    media_not_allowed_on_chat: 'Media not allowed on chat',
  };

  const label = $derived(LABELS[reason] ?? `Unknown reason: ${reason || 'missing'}`);
  const known = $derived(Boolean(LABELS[reason]));
</script>

<span class="inactive-reason" data-known={known}>{label}</span>

<style>
  .inactive-reason {
    display: inline-flex;
    align-items: center;
    min-height: 22px;
    padding: 2px 7px;
    border: 1px solid var(--warning, #b45309);
    border-radius: 6px;
    background: var(--warning-soft, rgba(180, 83, 9, 0.12));
    color: var(--text-primary, #111827);
    font-size: 0.6875rem;
    font-weight: 700;
    line-height: 1.2;
  }

  .inactive-reason[data-known='false'] {
    border-color: var(--danger, #b91c1c);
    color: var(--danger, #b91c1c);
  }
</style>
