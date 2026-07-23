<script>
  import { showToast } from '$lib/stores/toast.svelte.js';
  import CapabilityCard from '../components/capabilities/CapabilityCard.svelte';
  import InstallApprovalModal from '../components/capabilities/InstallApprovalModal.svelte';
  import HelpPopover from '$lib/components/help/HelpPopover.svelte';
  import HelpTooltip from '$lib/components/help/HelpTooltip.svelte';
  import { workbenchKernelRequest } from '$lib/api.js';
  let loading = $state(true);
  let actionPending = $state(false);
  let capabilities = $state([]);
  let error = $state(null);
  let activeMetadata = $state(null);
  let modalOpen = $state(false);
  let sortedCapabilities = $derived([...capabilities].sort((a, b) => a.display_name.localeCompare(b.display_name)));
  async function request(url, options = {}) {
    return workbenchKernelRequest(url, options);
  }
  async function loadCapabilities() {
    loading = true;
    try { capabilities = (await request('/api/v1/capabilities')).capabilities ?? []; error = null; }
    catch (err) { error = err.message; }
    finally { loading = false; }
  }
  async function openApprovalModal(kind) {
    actionPending = true;
    try { activeMetadata = await request(`/api/v1/capabilities/${kind}`); modalOpen = true; }
    catch (err) { error = err.message; showToast(`Capability details failed: ${err.message}`, 'error'); }
    finally { actionPending = false; }
  }
  async function approveInstall() {
    actionPending = true;
    try {
      const approval = await request(`/api/v1/capabilities/${activeMetadata.kind}/approve`, { method: 'POST', body: JSON.stringify({ approver_session_id: 'capabilities-view' }) });
      await request(`/api/v1/capabilities/${activeMetadata.kind}/install`, { method: 'POST', body: JSON.stringify({ request_id: approval.request_id, approved_at_utc: approval.approved_at_utc, approver_session_id: 'capabilities-view' }) });
      showToast(`${activeMetadata.display_name} installed`, 'success');
      modalOpen = false;
      await loadCapabilities();
    } catch (err) { error = err.message; showToast(`Capability install failed: ${err.message}`, 'error'); }
    finally { actionPending = false; }
  }
  async function declineInstall(reason = '') {
    actionPending = true;
    try {
      await request(`/api/v1/capabilities/${activeMetadata.kind}/decline`, { method: 'POST', body: JSON.stringify({ reason }) });
      modalOpen = false;
      await loadCapabilities();
    } catch (err) { error = err.message; showToast(`Capability decline failed: ${err.message}`, 'error'); }
    finally { actionPending = false; }
  }
  async function verifyHealth(kind) {
    await request(`/api/v1/capabilities/${kind}/probe`, { method: 'POST' });
    await loadCapabilities();
  }
  $effect(() => { loadCapabilities(); });
</script>

<div class="capabilities-view" data-testid="capabilities-view-root">
  <header><h2>Capabilities</h2><button type="button" onclick={loadCapabilities}>Refresh</button></header>
  {#if loading}<div>Loading capabilities...</div>
  {:else if error}<div role="alert">{error}</div>
  {:else}<section class="grid">{#each sortedCapabilities as capability (capability.kind)}<CapabilityCard {capability} onRequestInstall={openApprovalModal} onVerifyHealth={verifyHealth} />{/each}</section>{/if}
  <InstallApprovalModal isOpen={modalOpen} metadata={activeMetadata} {actionPending} onApprove={approveInstall} onDecline={declineInstall} onClose={() => { modalOpen = false; }} />
</div>

<style>
  .capabilities-view { display: flex; flex-direction: column; gap: 1rem; }
  header { display: flex; justify-content: space-between; gap: 1rem; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 1rem; }

  @media (max-width: 700px) {
    header {
      align-items: stretch;
      flex-direction: column;
    }

    header button {
      width: 100%;
    }

    .grid {
      grid-template-columns: 1fr;
    }
  }
</style>
