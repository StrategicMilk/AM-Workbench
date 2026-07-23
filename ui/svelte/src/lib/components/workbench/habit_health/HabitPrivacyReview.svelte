<script>
  import Icon from '$lib/a11y/Icon.svelte';

  let { store } = $props();

  let sourceContext = $state('user-visible habit review');
  let consentRef = $state('consent:habit-health-local');
  let contractRef = $state('');
  let deleteReason = $state('user-request');

  async function preview() {
    await store.previewDownstream({
      scope: 'personal_wellness',
      source_context: sourceContext,
      consent_refs: [consentRef],
      provenance_ref: 'ui:habit-health',
      downstream_use: 'personalization',
      allowed_downstream_uses: ['personalization'],
      downstream_contract_refs: contractRef ? [contractRef] : [],
    });
  }
</script>

<section class="panel privacy-review" aria-label="Habit privacy review">
  <h2>Privacy Review</h2>
  <label>
    <span>Source context</span>
    <input bind:value={sourceContext} />
  </label>
  <label>
    <span>Consent reference</span>
    <input bind:value={consentRef} />
  </label>
  <label>
    <span>Downstream contract</span>
    <input bind:value={contractRef} placeholder="required for use" />
  </label>
  <div class="actions">
    <button type="button" onclick={preview}>
      <Icon name="eye" />
      <span>Preview</span>
    </button>
    <button type="button" onclick={() => store.exportData({ source_context: sourceContext, consent_refs: [consentRef], provenance_ref: 'ui:habit-health', allowed_downstream_uses: ['export'], scope: 'personal_wellness' })}>
      <Icon name="file-export" />
      <span>Export</span>
    </button>
    <button type="button" onclick={() => store.deleteData(deleteReason)}>
      <Icon name="trash" />
      <span>Delete</span>
    </button>
  </div>
  {#if store.downstreamPreview}
    <article class:denied={!store.downstreamPreview.allowed}>
      <strong>{store.downstreamPreview.allowed ? 'Allowed' : 'Needs review'}</strong>
      <span>{(store.downstreamPreview.reasons ?? []).join(', ')}</span>
    </article>
  {/if}
</section>
