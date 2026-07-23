<script>
  import AriaLive from '$lib/a11y/aria_live.svelte';
  import { trapFocus } from '$lib/a11y/focus_trap';
  import { handleEscapeKey } from '$lib/a11y/keyboard_handlers';
  import InstallImpactPanel from './InstallImpactPanel.svelte';
  import { workbenchKernelRequest } from '$lib/api.js';
  let { isOpen = false, metadata = null, actionPending = false, onApprove = () => {}, onDecline = () => {}, onClose = () => {} } = $props();
  let reviewedImpact = $state(false);
  let probeResult = $state(null);
  let approveDisabled = $derived(actionPending || !reviewedImpact);
  let liveMessage = $derived(
    probeResult ? (probeResult.reachable ? 'Capability smoke test is reachable.' : probeResult.error) : ''
  );
  $effect(() => { if (isOpen) { reviewedImpact = false; probeResult = null; } });
  async function runProbe() {
    try {
      probeResult = await workbenchKernelRequest(`/api/v1/capabilities/${metadata.kind}/probe`, { method: 'POST' });
    } catch (err) {
      probeResult = { reachable: false, error: `Probe request failed: ${err instanceof Error ? err.message : String(err)}` };
    }
  }

  function closeModal() {
    if (!actionPending) onClose();
  }
</script>

{#if isOpen && metadata}
  <div class="backdrop" role="presentation" onclick={closeModal}>
    <div
      class="modal"
      role="dialog"
      aria-modal="true"
      aria-labelledby="install-approval-modal-title"
      aria-describedby="install-approval-modal-description"
      tabindex="-1"
      use:trapFocus={isOpen}
      onkeydown={(event) => handleEscapeKey(event, closeModal)}
      onclick={(event) => event.stopPropagation()}
    >
      <header>
        <h2 id="install-approval-modal-title">Approve capability install</h2>
        <button type="button" class="close-button" aria-label="Close install approval" onclick={closeModal} disabled={actionPending}>
          <i class="fas fa-xmark" aria-hidden="true"></i>
        </button>
      </header>
      <p id="install-approval-modal-description" class="modal-description">
        Review the install impact before approving this capability.
      </p>
      <div
        class="scroll"
        role="region"
        aria-label="Capability install impact"
        onscroll={() => { reviewedImpact = true; }}
      >
        <InstallImpactPanel {metadata} />
      </div>
      <label class="review-check">
        <input type="checkbox" bind:checked={reviewedImpact} />
        <span>I reviewed the install impact</span>
      </label>
      <button type="button" data-testid="install-approval-probe-button" onclick={runProbe}>Run smoke test first</button>
      <AriaLive message={liveMessage} />
      {#if probeResult}<p role={probeResult.reachable ? 'status' : 'alert'}>{probeResult.reachable ? 'Reachable' : probeResult.error}</p>{/if}
      <footer>
        <button type="button" data-testid="install-approval-decline-button" onclick={() => onDecline('not now')} disabled={actionPending}>Not now</button>
        <button type="button" data-testid="install-approval-approve-button" onclick={onApprove} disabled={approveDisabled}>Approve and install</button>
      </footer>
    </div>
  </div>
{/if}

<style>
  .backdrop { position: fixed; inset: 0; display: flex; align-items: center; justify-content: center; background: rgb(15 23 42 / 0.45); }
  .modal { width: min(760px, 94vw); max-height: 92vh; overflow: auto; border: 1px solid var(--border-color); border-radius: 8px; padding: 1rem; background: var(--background-color); }
  header, footer { display: flex; justify-content: space-between; gap: 1rem; }
  .close-button { min-width: 44px; min-height: 44px; display: inline-flex; align-items: center; justify-content: center; }
  .modal-description { margin: 0 0 0.75rem; color: var(--text-secondary, #4b5563); }
  .scroll { max-height: 58vh; overflow: auto; }
  .review-check { display: flex; align-items: center; gap: 0.5rem; margin: 0.75rem 0; }
</style>
