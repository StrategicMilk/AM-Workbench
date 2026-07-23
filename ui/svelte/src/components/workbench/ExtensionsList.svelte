<script>
  import Icon from '$lib/a11y/Icon.svelte';
  import { installExtension, listExtensions } from '$lib/api.js';

  let extensions = $state([]);
  let loading = $state(false);
  let installing = $state(false);
  let marketplaceExtensionId = $state('');
  let error = $state('');
  let status = $state('');
  const privacyNotice =
    'Marketplace installs send only the extension id and marketplace reference; no workspace content is shared during install.';

  async function refreshExtensions() {
    loading = true;
    error = '';
    try {
      const payload = await listExtensions();
      extensions = payload.extensions ?? [];
    } catch (err) {
      error = err.message ?? String(err);
    } finally {
      loading = false;
    }
  }

  async function installMarketplaceExtension() {
    const extensionId = marketplaceExtensionId.trim();
    if (!extensionId) {
      error = 'Select a marketplace extension before installing.';
      return;
    }

    installing = true;
    error = '';
    status = '';
    try {
      const payload = await installExtension({
        extension_id: extensionId,
        marketplace_ref: `marketplace:${extensionId}`,
      });
      status = payload.extension?.name ?? payload.extension?.extension_id ?? extensionId;
      marketplaceExtensionId = '';
      await refreshExtensions();
    } catch (err) {
      error = err.message ?? String(err);
    } finally {
      installing = false;
    }
  }

  $effect(() => {
    void refreshExtensions();
  });
</script>

<section class="extensions-list" data-testid="fsa0057-extensions-list" aria-label="Installed extensions">
  <div class="extensions-toolbar">
    <h2>Installed</h2>
    <button type="button" onclick={refreshExtensions} disabled={loading} aria-label="Refresh extensions" title="Refresh extensions">
      <Icon name={loading ? 'spinner' : 'rotate'} class={loading ? 'fa-spin' : ''} />
    </button>
  </div>

  <div class="extensions-grid">
    {#each extensions as extension (extension.name ?? extension.id)}
      <article class="extension-row">
        <div>
          <strong>{extension.name}</strong>
          <span>{extension.version ?? 'unknown'}</span>
        </div>
        <span>{(extension.capabilities ?? []).join(', ') || 'none'}</span>
      </article>
    {:else}
      <div class="empty">No installed extensions.</div>
    {/each}
  </div>

  <div class="install-row">
    <input
      bind:value={marketplaceExtensionId}
      type="text"
      autocomplete="off"
      inputmode="text"
      aria-label="Marketplace extension id"
      aria-describedby="extensions-install-privacy"
      placeholder="marketplace extension id"
    />
    <button type="button" onclick={installMarketplaceExtension} disabled={installing || !marketplaceExtensionId.trim()}>
      <Icon name={installing ? 'spinner' : 'plus'} class={installing ? 'fa-spin' : ''} />
      Install
    </button>
  </div>
  <p id="extensions-install-privacy" class="privacy-notice">{privacyNotice}</p>

  {#if status}
    <div class="extension-status" role="status">{status}</div>
  {/if}
  {#if error}
    <div class="extension-error" role="alert">{error}</div>
  {/if}
</section>

<style>
  .extensions-list {
    display: grid;
    gap: 12px;
    border: 1px solid var(--border-default);
    border-radius: 8px;
    background: var(--surface-elevated);
    padding: 14px;
  }

  .extensions-toolbar,
  .extension-row,
  .install-row {
    display: grid;
    gap: 8px;
    align-items: center;
  }

  .extensions-toolbar {
    grid-template-columns: minmax(0, 1fr) 38px;
  }

  h2 {
    margin: 0;
    font-size: 1rem;
  }

  .extensions-grid {
    display: grid;
    gap: 8px;
  }

  .extension-row {
    grid-template-columns: minmax(0, 1fr) minmax(100px, 0.6fr);
    border: 1px solid var(--border-subtle, var(--border-default));
    border-radius: 6px;
    padding: 8px;
  }

  .extension-row div {
    display: grid;
    gap: 2px;
    min-width: 0;
  }

  .extension-row span,
  .empty {
    color: var(--text-muted);
    font-size: 0.8rem;
    overflow-wrap: anywhere;
  }

  .install-row {
    grid-template-columns: minmax(0, 1fr) auto;
  }

  input,
  button {
    border: 1px solid var(--border-default);
    border-radius: 6px;
    background: var(--surface-bg);
    color: var(--text-primary);
    font: inherit;
  }

  input {
    min-width: 0;
    padding: 8px;
  }

  button {
    min-height: 36px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    padding: 7px 10px;
  }

  .extension-status {
    color: var(--success);
  }

  .privacy-notice {
    margin: -4px 0 0;
    color: var(--text-muted);
    font-size: 0.76rem;
    line-height: 1.35;
  }

  .extension-error {
    color: var(--danger);
  }

  @media (max-width: 760px) {
    .extension-row,
    .install-row {
      grid-template-columns: 1fr;
    }
  }
</style>
