<script>
  import VaultEntryPanel from './VaultEntryPanel.svelte';
  import RefinementJournalView from './RefinementJournalView.svelte';
  import RejectedEntriesQueue from './RejectedEntriesQueue.svelte';
  import { KnowledgeVaultStore } from './vault_store.svelte.js';

  const store = new KnowledgeVaultStore();
  let activeTab = $state('entries');
  let vaultError = $state('');
  let entries = $derived(Array.isArray(store.entries) ? store.entries : []);
  let groupedEntries = $derived(Object.groupBy(entries, (entry) => entry.kind ?? 'unknown'));
  let vaultReceipt = $derived(`rcg-0021-p05:vault:${vaultError ? 'blocked' : 'ready'}`);

  async function loadVaultState() {
    vaultError = '';
    try {
      await Promise.all([store.loadEntries(), store.loadRejected(), store.loadJournal()]);
    } catch (err) {
      vaultError = err instanceof Error ? err.message : String(err);
    }
  }

  async function reverseEntry(eventId, reason) {
    if (!reason.trim()) {
      vaultError = 'Reverse reason is required before the journal can be changed.';
      return;
    }
    try {
      vaultError = '';
      await store.reverseJournalEntry(eventId, reason);
    } catch (err) {
      vaultError = err instanceof Error ? err.message : String(err);
    }
  }

  $effect(() => {
    loadVaultState();
  });
</script>

<section class="vault-explorer" aria-label="Knowledge Vault" data-rcg0021-p05-state={vaultError ? 'blocked' : 'ready'}>
  <header>
    <div>
      <h2>Knowledge Vault</h2>
      <p>{entries.length} exported entries</p>
      <p>{vaultReceipt}</p>
    </div>
    <div class="controls">
      <select bind:value={store.filters.requested_scope} aria-label="Export scope">
        <option value="shareable">Shareable</option>
        <option value="private">Private</option>
        <option value="sensitive">Sensitive</option>
      </select>
      <button onclick={() => store.triggerExport()}>Export</button>
      <button onclick={() => store.triggerRebuild()}>Rebuild</button>
    </div>
  </header>

  <nav class="tabs" aria-label="Vault tabs">
    <button class:active={activeTab === 'entries'} onclick={() => { activeTab = 'entries'; }}>Entries</button>
    <button class:active={activeTab === 'rejected'} onclick={() => { activeTab = 'rejected'; }}>Rejected</button>
    <button class:active={activeTab === 'journal'} onclick={() => { activeTab = 'journal'; }}>Journal</button>
  </nav>

  {#if vaultError}
    <div class="status-banner" role="alert" aria-live="assertive">{vaultError}</div>
  {/if}

  {#if activeTab === 'entries'}
    <div class="vault-grid">
      <section class="entry-list" aria-label="Vault entries">
        {#each Object.entries(groupedEntries) as [kind, grouped]}
          <h3>{kind}</h3>
          {#each grouped as entry (entry.entry_id)}
            <button class:selected={store.selectedEntry?.entry_id === entry.entry_id} onclick={() => store.selectEntry(entry)}>
              <strong>{entry.title}</strong>
              <span>{entry.slug}</span>
            </button>
          {/each}
        {/each}
      </section>
      <VaultEntryPanel entry={store.selectedEntry} />
    </div>
  {:else if activeTab === 'rejected'}
    <RejectedEntriesQueue entries={store.rejectedEntries} />
  {:else}
    <RefinementJournalView entries={store.journalEntries} onReverse={reverseEntry} />
  {/if}
</section>

<style>
  .vault-explorer { padding: 18px; display: flex; flex-direction: column; gap: 14px; max-width: 1440px; }
  header { display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; flex-wrap: wrap; }
  h2, h3, p { margin: 0; }
  h2 { font-size: 1.25rem; color: var(--text-primary); }
  h3 { color: var(--text-muted); font-size: 0.82rem; text-transform: uppercase; margin-top: 8px; }
  p { color: var(--text-muted); font-size: 0.84rem; }
  .controls, .tabs { display: flex; gap: 8px; flex-wrap: wrap; }
  button, select { border: 1px solid var(--border-default); border-radius: 6px; background: var(--surface-elevated); color: var(--text-primary); padding: 8px 10px; }
  button.active, button.selected { border-color: var(--accent, #4f9cf9); }
  .status-banner { border: 1px solid #d44d4d; border-radius: 8px; padding: 10px 12px; }
  .vault-grid { display: grid; grid-template-columns: minmax(260px, 0.7fr) minmax(420px, 1fr); gap: 12px; align-items: start; }
  .entry-list { display: flex; flex-direction: column; gap: 8px; }
  .entry-list button { text-align: left; display: grid; gap: 3px; }
  .entry-list span { color: var(--text-muted); font-size: 0.78rem; }
  @media (max-width: 900px) { .vault-grid { grid-template-columns: 1fr; } }
</style>
