<script>
  import RiskBadge from './RiskBadge.svelte';
  let { metadata = {} } = $props();
  let credentials = $derived(metadata.requires_credentials ?? []);
  let packages = $derived(metadata.extra_packages ?? []);
</script>

<section class="impact-panel">
  <header><h3>{metadata.display_name ?? metadata.kind}</h3><RiskBadge risk={metadata.risk_level ?? 'moderate'} /></header>
  <dl>
    <div data-testid="install-impact-target-env"><dt>Target environment</dt><dd>{metadata.target_environment}</dd></div>
    <div data-testid="install-impact-disk-mb"><dt>Disk impact</dt><dd>{metadata.disk_impact_mb} MB</dd></div>
    <div data-testid="install-impact-network-mb"><dt>Network impact</dt><dd>{metadata.network_impact_mb} MB</dd></div>
    <div data-testid="install-impact-native"><dt>Native binaries</dt><dd>{metadata.requires_native_binary ? 'Required' : 'Not required'}</dd></div>
    <div data-testid="install-impact-wsl"><dt>WSL</dt><dd>{metadata.requires_wsl ? 'Required' : 'Not required'}</dd></div>
    <div data-testid="install-impact-credentials"><dt>Credentials</dt><dd>{credentials.length ? credentials.join(', ') : 'None'}</dd></div>
  </dl>
  {#if packages.length}<p>{packages.join(', ')}</p>{/if}
  {#if credentials.length}<div data-testid="install-impact-credentials-warning">Credentials required: {credentials.join(', ')}</div>{/if}
  <p data-testid="install-impact-degraded-fallback">{metadata.degraded_fallback}</p>
  <p>{metadata.uninstall_note}</p>
</section>

<style>
  .impact-panel { display: flex; flex-direction: column; gap: 0.75rem; }
  header { display: flex; justify-content: space-between; gap: 1rem; }
  dl { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 0.75rem; margin: 0; }
  dl div { border: 1px solid var(--border-color); border-radius: 8px; padding: 0.75rem; }
  dd { margin: 0.25rem 0 0; font-weight: 700; }
</style>
