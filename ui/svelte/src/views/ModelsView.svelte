<script>
  /**
   * Models management view — browse, search, download, and select models.
   *
   * Local tab: models from /api/v1/models (discovered on disk).
   * Discover tab: curated models from /api/v1/models/popular merged with
   * on-demand HuggingFace search via /api/v1/models/search.
   */
  import ModelCard from '$components/models/ModelCard.svelte';
  import ModelDownload from '$components/models/ModelDownload.svelte';
  import ModelRecommendations from '$components/models/ModelRecommendations.svelte';
  import BackendStatus from '$components/models/BackendStatus.svelte';
  import AgentStreamPanel from '$components/models/AgentStreamPanel.svelte';
  import ModelHubBrowser from '$components/workbench/ModelHubBrowser.svelte';
  import * as api from '$lib/api.js';
  import { showToast } from '$lib/stores/toast.svelte.js';
  import HelpPopover from '$lib/components/help/HelpPopover.svelte';
  import HelpTooltip from '$lib/components/help/HelpTooltip.svelte';

  let models = $state([]);
  let catalog = $state([]);
  let searchResults = $state([]);
  let recommendations = $state([]);
  let loading = $state(true);
  /** Set to a message string when ALL primary fetches fail; null when at least one succeeds. */
  let fetchError = $state(null);
  let searching = $state(false);
  let searchQuery = $state('');
  let selectedDownload = $state(null);
  let activeTab = $state('local');
  let activeModality = $state('text');

  let filteredModels = $derived(
    searchQuery.length === 0
      ? models
      : models.filter((m) =>
          (m.name ?? m.id ?? '').toLowerCase().includes(searchQuery.toLowerCase())
        )
  );

  // Discover tab: merge curated catalog with search results, deduplicated
  let discoverModels = $derived.by(() => {
    const seen = new Set();
    const merged = [];
    function addKey(m) {
      // Dedup by repo_id, id, AND normalized name to catch duplicates
      const keys = [m.repo_id, m.id, m.name?.toLowerCase()].filter(Boolean);
      if (keys.some((k) => seen.has(k))) return false;
      keys.forEach((k) => seen.add(k));
      return true;
    }
    // Search results first (more relevant)
    for (const m of searchResults) {
      if (addKey(m)) merged.push(m);
    }
    // Then curated catalog entries
    for (const m of catalog) {
      if (addKey(m)) merged.push(m);
    }
    return merged;
  });

  async function loadModels() {
    fetchError = null;
    try {
      // Use null sentinels so we can tell a failed fetch from an empty response.
      const [modelsData, popularData] = await Promise.all([
        api.listModels().catch(() => null),
        api.getPopularModels({ modality: activeModality }).catch(() => null),
      ]);

      // If both primary fetches failed the backend is unreachable — show an
      // error state rather than the misleading "No local models found" message.
      if (modelsData === null && popularData === null) {
        fetchError = 'Could not load models — backend may be down or restarting.';
      } else {
        models = modelsData?.models ?? (Array.isArray(modelsData) ? modelsData : []);
        catalog = (popularData?.models ?? []).map((m) => ({
          ...m,
          id: m.repo_id || m.name,
          status: m.status || 'remote',
        }));

        // Recommendations are best-effort; failure leaves the list empty, not errored.
        try {
          const scored = await api.scoreModels();
          recommendations = scored?.recommendations ?? scored?.models ?? [];
        } catch {
          recommendations = [];
        }
      }
    } catch (err) {
      fetchError = `Failed to load models: ${err.message}`;
    } finally {
      loading = false;
    }
  }

  async function handleSearch() {
    const query = searchQuery.trim();
    if (!query || query.length < 2) {
      searchResults = [];
      return;
    }
    if (activeTab !== 'discover') return;

    searching = true;
    try {
      const result = await api.searchModels(query);
      // Filter out local-source candidates (already in catalog) and map to card format
      searchResults = (result?.candidates ?? [])
        .filter((c) => c.source_type !== 'local')
        .map((c) => ({
          ...c,
          id: c.id || c.name,
          repo_id: c.id,
          status: 'remote',
          format: 'safetensors',
        }));
    } catch {
      // Search failed silently — curated list still visible
    } finally {
      searching = false;
    }
  }

  async function handleSelectModel(modelId) {
    try {
      await api.selectModel(modelId);
      showToast('Model selected', 'success');
      await loadModels();
    } catch (err) {
      showToast(`Selection failed: ${err.message}`, 'error');
    }
  }

  async function handleRemoveModel(modelId, modelName) {
    if (!modelId) return;
    const confirmed = confirm(
      `Remove model "${modelName}" from the local relay catalog? This cannot be undone from the UI.`
    );
    if (!confirmed) return;

    try {
      await api.workbenchKernelRequest(`/api/v1/models/${encodeURIComponent(modelId)}/delete`, {
        method: 'POST',
        body: JSON.stringify({
          confirmed_by: 'local-ui',
          reason: `User confirmed model removal for ${modelId}`,
          confirmed_at_utc: new Date().toISOString(),
        }),
      });
      showToast('Model removed', 'info');
      await loadModels();
    } catch (err) {
      showToast(`Remove failed: ${err.message}`, 'error');
    }
  }

  async function handleRefresh() {
    loading = true;
    try {
      await api.refreshModels();
      await loadModels();
      showToast('Models refreshed', 'success');
    } catch (err) {
      showToast(`Refresh failed: ${err.message}`, 'error');
      loading = false;
    }
  }

  function selectForDownload(model) {
    selectedDownload = model;
  }

  // Debounced search on query change (Discover tab only)
  let searchTimer;
  $effect(() => {
    const q = searchQuery;
    clearTimeout(searchTimer);
    if (q.trim().length >= 2 && activeTab === 'discover') {
      searchTimer = setTimeout(() => handleSearch(), 400);
    } else {
      searchResults = [];
    }
    return () => clearTimeout(searchTimer);
  });

  $effect(() => { loadModels(); });
</script>

<div class="models-view">
  <div class="models-header">
    <h2>
      <i class="fas fa-microchip" aria-hidden="true"></i>
      Models
    </h2>
    <div class="models-actions">
      <div class="search-container">
        <i class="fas fa-search search-icon" aria-hidden="true"></i>
        <input
          type="text"
          class="input search-input"
          placeholder={activeTab === 'discover' ? 'Search HuggingFace...' : 'Search models...'}
          bind:value={searchQuery}
          aria-label="Search models"
        />
        {#if searching}
          <i class="fas fa-spinner fa-spin search-spinner" aria-hidden="true"></i>
        {/if}
      </div>
      <button class="btn btn-secondary" onclick={handleRefresh} disabled={loading}>
        <i class="fas fa-sync-alt" class:fa-spin={loading} aria-hidden="true"></i>
        Refresh
      </button>
    </div>
  </div>

  <!-- Tab switcher -->
  <div class="tab-bar" role="tablist" tabindex="0" onkeydown={(e) => {
    const tabs = ['local', 'discover'];
    const idx = tabs.indexOf(activeTab);
    if (e.key === 'ArrowRight') { e.preventDefault(); activeTab = tabs[(idx + 1) % tabs.length]; }
    else if (e.key === 'ArrowLeft') { e.preventDefault(); activeTab = tabs[(idx - 1 + tabs.length) % tabs.length]; }
  }}>
      <button
      class="tab" class:active={activeTab === 'local'}
      role="tab" aria-selected={activeTab === 'local'}
      aria-controls="models-panel"
      tabindex={activeTab === 'local' ? 0 : -1}
      onclick={() => { activeTab = 'local'; }}
    >
      Local Models ({filteredModels.length})
    </button>
    <button
      class="tab" class:active={activeTab === 'discover'}
      role="tab" aria-selected={activeTab === 'discover'}
      aria-controls="models-panel"
      tabindex={activeTab === 'discover' ? 0 : -1}
      onclick={() => { activeTab = 'discover'; }}
    >
      Discover ({discoverModels.length})
    </button>
  </div>

  <div class="models-content" id="models-panel" role="tabpanel">
    <div class="models-main">
      {#if loading}
        <div class="models-loading">
          <i class="fas fa-spinner fa-spin"></i>
          Loading models...
        </div>
      {:else if fetchError}
        <div class="models-fetch-error" role="alert" aria-live="assertive">
          <i class="fas fa-exclamation-triangle"></i>
          <p>{fetchError}</p>
          <button class="btn btn-secondary" onclick={loadModels}>Retry</button>
        </div>
      {:else if activeTab === 'local'}
        {#if filteredModels.length === 0}
          <div class="models-empty">
            <i class="fas fa-box-open"></i>
            <p>No local models found</p>
            <button class="btn btn-primary" onclick={() => { activeTab = 'discover'; }}>
              Discover Models
            </button>
          </div>
        {:else}
          <div class="model-grid">
            {#each filteredModels as model (model.id)}
              <ModelCard {model} onselect={handleSelectModel} onremove={handleRemoveModel} />
            {/each}
          </div>
        {/if}
      {:else}
        {#if discoverModels.length === 0}
          <div class="models-empty">
            <i class="fas fa-search" aria-hidden="true"></i>
            <p>No models found — try searching HuggingFace above</p>
          </div>
        {:else}
          <div class="model-grid">
            {#each discoverModels as model (model.id ?? model.name)}
              <div>
                <ModelCard {model} />
                <button
                  class="btn btn-small btn-secondary download-btn"
                  onclick={() => selectForDownload(model)}
                  aria-label={`Download model ${model.name ?? model.id ?? 'unknown'}`}
                >
                  <i class="fas fa-download" aria-hidden="true"></i>
                  Download
                </button>
              </div>
            {/each}
          </div>
        {/if}
      {/if}
    </div>

    <div class="models-sidebar">
      <BackendStatus />
      <AgentStreamPanel />
      <ModelRecommendations {recommendations} onselect={handleSelectModel} />
      <ModelHubBrowser />
      {#if selectedDownload}
        <ModelDownload
          model={selectedDownload}
          onclose={() => { selectedDownload = null; }}
        />
      {/if}
    </div>
  </div>
</div>

<style>
  .models-view { padding: 24px; max-width: 1400px; }

  .models-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 20px;
    flex-wrap: wrap;
    gap: 12px;
  }

  .models-header h2 {
    font-size: 1.25rem;
    font-weight: 600;
    color: var(--text-primary);
    display: flex;
    align-items: center;
    gap: 8px;
    margin: 0;
  }

  .models-header h2 i { color: var(--secondary); }

  .models-actions {
    display: flex;
    align-items: center;
    gap: 12px;
  }

  .search-container {
    position: relative;
  }

  .search-spinner {
    position: absolute;
    right: 10px;
    top: 50%;
    transform: translateY(-50%);
    color: var(--text-muted);
    font-size: 0.75rem;
  }

  .tab-bar {
    display: flex;
    gap: 4px;
    margin-bottom: 20px;
    border-bottom: 1px solid var(--border-default);
    padding-bottom: 0;
  }

  .tab {
    padding: 10px 20px;
    background: none;
    border: none;
    border-bottom: 2px solid transparent;
    color: var(--text-muted);
    font-size: 0.875rem;
    font-weight: 500;
    cursor: pointer;
    font-family: inherit;
    transition: color 150ms, border-color 150ms;
  }

  .tab.active {
    color: var(--primary);
    border-bottom-color: var(--primary);
  }

  .tab:hover { color: var(--text-primary); }

  .models-content {
    display: grid;
    grid-template-columns: 1fr 300px;
    gap: 20px;
  }

  .model-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
    gap: 12px;
  }

  .models-sidebar {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  .models-loading, .models-empty {
    text-align: center;
    padding: 48px 24px;
    color: var(--text-muted);
    font-size: 0.9375rem;
  }

  .models-empty i {
    font-size: 2rem;
    margin-bottom: 12px;
    display: block;
  }

  /* Shown when all primary fetches fail — prevents "No local models found" on error */
  .models-fetch-error {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 10px;
    padding: 48px 24px;
    color: var(--danger);
    font-size: 0.9375rem;
    text-align: center;
  }

  .models-fetch-error i {
    font-size: 2rem;
  }

  .download-btn {
    margin-top: 8px;
    min-height: 44px;
    width: 100%;
  }

  @media (max-width: 1024px) {
    .models-content { grid-template-columns: 1fr; }
  }
</style>
