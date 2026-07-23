<script>
  import RiskBadge from './RiskBadge.svelte';
  let { capability, onRequestInstall = () => {}, onVerifyHealth = () => {} } = $props();
  const labels = { not_installed: 'Install...', install_failed: 'Re-attempt install...', declined_for_now: 'Install now', installed: 'Verify health' };
  let safeCapability = $derived(capability ?? {});
  let capabilityKind = $derived(String(safeCapability.kind ?? 'unknown'));
  let displayName = $derived(String(safeCapability.display_name ?? capabilityKind));
  let actionLabel = $derived(labels[safeCapability.install_state] ?? 'Install...');
</script>

<article class="capability-card" data-testid={`capability-card-${capabilityKind}`}>
  <header><h3>{displayName}</h3><RiskBadge risk={safeCapability.risk_level} /></header>
  <p>{safeCapability.target_environment ?? 'target unavailable'}</p>
  <p role="status" aria-live="polite" aria-label={`Capability status ${safeCapability.install_state ?? 'unknown'} and health ${safeCapability.health_state ?? 'unknown'}`}>
    {safeCapability.install_state ?? 'unknown'} / {safeCapability.health_state ?? 'unknown'}
  </p>
  <button
    type="button"
    aria-label={`${actionLabel.replace('...', '')} ${displayName}`}
    onclick={() => safeCapability.install_state === 'installed' ? onVerifyHealth(capabilityKind) : onRequestInstall(capabilityKind)}
  >
    {actionLabel}
  </button>
</article>

<style>
  .capability-card { display: flex; flex-direction: column; gap: 0.75rem; border: 1px solid var(--border-color); border-radius: 8px; padding: 1rem; background: var(--surface-color); }
  header { display: flex; justify-content: space-between; gap: 1rem; }
  h3, p { margin: 0; }
  button { min-height: 44px; }
</style>
