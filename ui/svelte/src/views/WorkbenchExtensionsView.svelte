<script>
  import { ExtensionMarketplacePanel } from '$lib/components/workbench/extensions';
  import ExtensionsList from '$components/workbench/ExtensionsList.svelte';
  import HelpPopover from '$lib/components/help/HelpPopover.svelte';
  import HelpTooltip from '$lib/components/help/HelpTooltip.svelte';
  import ProvenanceGate from '$lib/security/ProvenanceGate.svelte';

  let pageState = $state({ disabled: true, risk: 'backend-authoritative' });
</script>

<svelte:head>
  <title>Workbench Extensions - AM Workbench</title>
</svelte:head>

<div class="workbench-extensions-view" data-risk={pageState.risk} data-disabled={pageState.disabled}>
  <header class="extensions-header">
    <div>
      <h1>Extensions</h1>
      <p>Browse and manage workbench extensions from the marketplace.</p>
      <HelpPopover
        title="Extensions"
        body="Extensions add capabilities to the workbench via the marketplace. Trust tiers: official (AM Workbench team), verified (audited third-party), community (unaudited — use with care). Risk classification is backend-authoritative: the backend assigns a risk level to each extension; the UI reflects that classification and cannot override it. Scoped secrets: extensions declare the secret names they require; the workbench provisions only those named secrets — no additional access is granted. Manual enablement: extensions are disabled by default after install; you must explicitly enable each one after reviewing its permissions."
        severity="warning"
      />
      <ProvenanceGate
        refs={['extension-marketplace:catalog', 'ui/svelte/src/lib/components/workbench/extensions/ExtensionMarketplacePanel.svelte']}
        status="verified"
        context="extension-marketplace-boundary"
      />
    </div>
  </header>
  <ExtensionMarketplacePanel />
  <ExtensionsList />
</div>

<style>
  .workbench-extensions-view {
    min-height: 100%;
    background: var(--surface-default, #101620);
  }
  .extensions-header { padding: 20px 20px 0; }
  .extensions-header h1 { margin: 0 0 4px; font-size: 24px; color: var(--text-primary); }
  .extensions-header p { margin: 0; color: var(--text-muted); }

  @media (max-width: 720px) {
    .workbench-extensions-view {
      overflow-x: hidden;
    }

    .extensions-header {
      padding: 16px 16px 0;
    }

    .extensions-header h1 {
      font-size: 20px;
    }
  }
</style>
