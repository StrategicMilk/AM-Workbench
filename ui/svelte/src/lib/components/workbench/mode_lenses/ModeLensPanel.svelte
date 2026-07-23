<script>
  import { onMount } from 'svelte';
  import Icon from '$lib/a11y/Icon.svelte';

  let { projectId = 'default', modeLensState = null, onModeLensChange = null } = $props();

  const lenses = [
    {
      id: 'casual_chat',
      label: 'Casual Chat',
      mode: 'casual',
      rigor: 'just_talk',
      memory: 'Pinned context only',
      tone: 'Plain',
      targets: ['creative_exploration', 'professional_assistance', 'life_admin', 'research', 'structured_workbench'],
      artifacts: ['pinned_context', 'conversion_manifest'],
      sensitive: 'Reject sensitive advice until authority and evidence exist',
    },
    {
      id: 'creative_exploration',
      label: 'Creative Exploration',
      mode: 'creative_writing',
      rigor: 'make_something',
      memory: 'Bible refs and branch ids',
      tone: 'Imaginative',
      targets: ['casual_chat', 'professional_assistance', 'research', 'structured_workbench'],
      artifacts: ['story_bible', 'scene_beats', 'draft_branches'],
      sensitive: 'Keep fictional or require proof',
    },
    {
      id: 'professional_assistance',
      label: 'Professional Assistance',
      mode: 'writing',
      rigor: 'help_me_think',
      memory: 'Verified draft branches',
      tone: 'Concise',
      targets: ['casual_chat', 'life_admin', 'research', 'structured_workbench'],
      artifacts: ['audience_style_template', 'redline_plan', 'export_manifest'],
      sensitive: 'Reject advice without authority and evidence',
    },
    {
      id: 'life_admin',
      label: 'Life Admin',
      mode: 'chat',
      rigor: 'help_me_think',
      memory: 'Accepted evidence ids',
      tone: 'Organized',
      targets: ['casual_chat', 'professional_assistance', 'research', 'structured_workbench'],
      artifacts: ['intake_decision', 'source_card_ids', 'evidence_asset_ids'],
      sensitive: 'Collect facts only until proven',
    },
    {
      id: 'research',
      label: 'Research',
      mode: 'research',
      rigor: 'check_it_carefully',
      memory: 'Source cards and claim ledgers',
      tone: 'Sourced',
      targets: ['casual_chat', 'professional_assistance', 'creative_exploration', 'structured_workbench'],
      artifacts: ['source_plan', 'claim_ledger', 'confidence_summary'],
      sensitive: 'Require sources and uncertainty',
    },
    {
      id: 'structured_workbench',
      label: 'Structured Workbench',
      mode: 'chat',
      rigor: 'make_it_reusable',
      memory: 'Evidence ids and signed decisions',
      tone: 'Explicit',
      targets: ['casual_chat', 'professional_assistance', 'research'],
      artifacts: ['verification_commands', 'evidence_asset_ids', 'operator_decision'],
      sensitive: 'Fail closed until operator proof exists',
    },
  ];

  const CONTINUITY_STATE_VERSION = 1;
  const CONTINUITY_STALE_AFTER_MS = 7 * 24 * 60 * 60 * 1000;

  let selectedId = $state('casual_chat');
  let continuityState = $state({
    status: 'blocked',
    label: 'Continuity unavailable',
    blockers: ['continuity_state_not_loaded'],
    receiptId: '',
  });
  let hydrated = $state(false);
  let lastPersistedLensId = $state('');
  let active = $derived(lenses.find((lens) => lens.id === selectedId) ?? lenses[0]);
  let continuityLabel = $derived(continuityState.label);
  let continuityDetail = $derived(
    continuityState.blockers.length > 0 ? continuityState.blockers.join(', ') : continuityState.receiptId
  );

  function storageKey() {
    return `vetinari:mode-lens:${projectId || 'default'}`;
  }

  function isRecord(value) {
    return value !== null && typeof value === 'object' && !Array.isArray(value);
  }

  function validLensId(value) {
    return lenses.some((lens) => lens.id === value) ? value : null;
  }

  function blockedContinuity(label, blockers, source = 'component') {
    return {
      status: label === 'Continuity stale' ? 'stale' : 'blocked',
      label,
      blockers,
      receiptId: '',
      selectedId: null,
      source,
    };
  }

  function normalizeContinuityState(value, source = 'external') {
    if (!isRecord(value)) {
      return blockedContinuity('Continuity unavailable', ['missing_mode_lens_state'], source);
    }

    const lensId = validLensId(value.lens_id ?? value.lensId ?? value.selected_id ?? value.selectedId);
    const persistedAt = value.persisted_at ?? value.persistedAt;
    const persistedAtMs = Date.parse(String(persistedAt ?? ''));
    const blockers = [];

    if (!lensId) blockers.push('invalid_lens_id');
    if (!Number.isFinite(persistedAtMs)) blockers.push('missing_persisted_at');

    const stale = Number.isFinite(persistedAtMs) && Date.now() - persistedAtMs > CONTINUITY_STALE_AFTER_MS;
    if (stale) blockers.push('stale_mode_lens_state');

    if (blockers.length > 0) {
      return blockedContinuity(stale ? 'Continuity stale' : 'Continuity blocked', blockers, source);
    }

    return {
      status: 'preserved',
      label: 'Branch preserved',
      blockers: [],
      receiptId:
        value.receipt_id ??
        value.receiptId ??
        `mode-lens:${projectId || 'default'}:${lensId}:${new Date(persistedAtMs).toISOString()}`,
      selectedId: lensId,
      source,
    };
  }

  function readPersistedContinuity() {
    if (typeof window === 'undefined') {
      return blockedContinuity('Continuity unavailable', ['browser_storage_unavailable'], 'localStorage');
    }

    try {
      const raw = window.localStorage.getItem(storageKey());
      if (!raw) {
        return blockedContinuity('Continuity unavailable', ['missing_mode_lens_state'], 'localStorage');
      }
      return normalizeContinuityState(JSON.parse(raw), 'localStorage');
    } catch (error) {
      return blockedContinuity('Continuity blocked', ['unreadable_mode_lens_state', String(error)], 'localStorage');
    }
  }

  function persistSelectedLens(lensId) {
    const normalized = validLensId(lensId);
    if (!normalized) {
      continuityState = blockedContinuity('Continuity blocked', ['invalid_lens_id'], 'component');
      return;
    }

    const persistedAt = new Date().toISOString();
    const payload = {
      version: CONTINUITY_STATE_VERSION,
      project_id: projectId || 'default',
      lens_id: normalized,
      status: 'preserved',
      persisted_at: persistedAt,
      receipt_id: `mode-lens:${projectId || 'default'}:${normalized}:${persistedAt}`,
    };

    try {
      if (typeof window !== 'undefined') {
        window.localStorage.setItem(storageKey(), JSON.stringify(payload));
      }
      continuityState = normalizeContinuityState(payload, 'component');
      lastPersistedLensId = normalized;
      onModeLensChange?.(payload);
    } catch (error) {
      continuityState = blockedContinuity('Continuity blocked', ['mode_lens_state_write_failed', String(error)], 'localStorage');
    }
  }

  onMount(() => {
    const restored = modeLensState ? normalizeContinuityState(modeLensState, 'external') : readPersistedContinuity();
    if (restored.selectedId) {
      selectedId = restored.selectedId;
      lastPersistedLensId = restored.selectedId;
    }
    continuityState = restored;
    hydrated = true;
  });

  $effect(() => {
    if (!hydrated || !modeLensState) return;
    const incoming = normalizeContinuityState(modeLensState, 'external');
    if (incoming.selectedId) {
      selectedId = incoming.selectedId;
      lastPersistedLensId = incoming.selectedId;
    }
    continuityState = incoming;
  });

  $effect(() => {
    if (!hydrated || selectedId === lastPersistedLensId) return;
    persistSelectedLens(selectedId);
  });
</script>

<section class="mode-lens-panel" data-testid="mode-lens-panel" data-project-id={projectId}>
  <header class="panel-header">
    <div>
      <h1>Mode Lenses</h1>
      <p>One conversation, different operating lenses.</p>
    </div>
    <div class="continuity" aria-live="polite" data-continuity-status={continuityState.status}>
      <span>Continuity</span>
      <strong>{continuityLabel}</strong>
      {#if continuityDetail}
        <small>{continuityDetail}</small>
      {/if}
    </div>
  </header>

  <div class="lens-layout">
    <nav class="lens-list" aria-label="Mode lens choices">
      {#each lenses as lens}
        <button
          type="button"
          class:active={selectedId === lens.id}
          aria-pressed={selectedId === lens.id}
          onclick={() => (selectedId = lens.id)}
        >
          <span>{lens.label}</span>
          <small>{lens.rigor}</small>
        </button>
      {/each}
    </nav>

    <div class="lens-detail">
      <div class="detail-top">
        <div>
          <span class="eyebrow">{active.mode}</span>
          <h2>{active.label}</h2>
        </div>
        <span class="rigor-pill">{active.rigor}</span>
      </div>

      <dl class="facts">
        <div>
          <dt>Tone</dt>
          <dd>{active.tone}</dd>
        </div>
        <div>
          <dt>Memory</dt>
          <dd>{active.memory}</dd>
        </div>
        <div>
          <dt>Sensitive Domains</dt>
          <dd>{active.sensitive}</dd>
        </div>
      </dl>

      <section class="token-section" aria-label="Artifact suggestions">
        <h3>Artifacts</h3>
        <div class="token-row">
          {#each active.artifacts as artifact}
            <span>{artifact}</span>
          {/each}
        </div>
      </section>

      <section class="token-section" aria-label="Transition targets">
        <h3>Transitions</h3>
        <div class="target-grid">
          {#each active.targets as target}
            <button type="button" disabled={target === active.id} onclick={() => (selectedId = target)}>
              <Icon name="arrow-right" />
              <span>{target.replaceAll('_', ' ')}</span>
            </button>
          {/each}
        </div>
      </section>
    </div>
  </div>
</section>

<style>
  .mode-lens-panel {
    display: grid;
    gap: 16px;
    max-width: 1180px;
    padding: 18px;
    color: var(--text-primary, #111827);
  }

  .panel-header {
    display: flex;
    justify-content: space-between;
    gap: 16px;
    align-items: flex-start;
  }

  h1,
  h2,
  h3,
  p,
  dl,
  dd {
    margin: 0;
  }

  h1 {
    font-size: 1.45rem;
  }

  .panel-header p,
  dt,
  small,
  .eyebrow {
    color: var(--text-secondary, #64748b);
  }

  .continuity {
    display: grid;
    gap: 4px;
    min-width: 150px;
    border: 1px solid var(--border-color, #d1d5db);
    border-radius: 8px;
    padding: 10px;
    background: var(--surface-secondary, #f8fafc);
    font-size: 0.85rem;
  }

  .continuity[data-continuity-status='blocked'],
  .continuity[data-continuity-status='stale'] {
    border-color: var(--warning, #f59e0b);
  }

  .continuity small {
    color: var(--text-secondary, #64748b);
    overflow-wrap: anywhere;
  }

  .lens-layout {
    display: grid;
    grid-template-columns: 260px minmax(0, 1fr);
    gap: 14px;
  }

  .lens-list {
    display: grid;
    gap: 8px;
    align-content: start;
  }

  .lens-list button,
  .target-grid button {
    border: 1px solid var(--border-color, #d1d5db);
    border-radius: 8px;
    background: var(--surface-primary, #fff);
    color: inherit;
    font: inherit;
    cursor: pointer;
  }

  .lens-list button {
    display: grid;
    gap: 3px;
    min-height: 54px;
    padding: 10px;
    text-align: left;
  }

  .lens-list button.active {
    border-color: var(--accent-color, #2563eb);
    background: var(--accent-subtle, #eff6ff);
  }

  .lens-detail {
    display: grid;
    gap: 14px;
    border: 1px solid var(--border-color, #d1d5db);
    border-radius: 8px;
    padding: 14px;
    background: var(--surface-secondary, #f8fafc);
  }

  .detail-top {
    display: flex;
    justify-content: space-between;
    gap: 12px;
  }

  .eyebrow {
    display: block;
    margin-bottom: 3px;
    font-size: 0.76rem;
    font-weight: 700;
    text-transform: uppercase;
  }

  .rigor-pill,
  .token-row span {
    border: 1px solid var(--border-color, #d1d5db);
    border-radius: 999px;
    background: var(--surface-primary, #fff);
    padding: 5px 9px;
    font-size: 0.78rem;
    font-weight: 700;
    white-space: nowrap;
  }

  .facts {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 10px;
  }

  .facts div {
    display: grid;
    gap: 5px;
    min-height: 72px;
    border: 1px solid var(--border-color, #d1d5db);
    border-radius: 8px;
    padding: 10px;
    background: var(--surface-primary, #fff);
  }

  dt,
  h3 {
    font-size: 0.78rem;
    font-weight: 700;
  }

  dd {
    line-height: 1.35;
  }

  .token-section {
    display: grid;
    gap: 8px;
  }

  .token-row,
  .target-grid {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }

  .target-grid button {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    min-height: 34px;
    padding: 0 10px;
    text-transform: capitalize;
  }

  .target-grid button:hover,
  .target-grid button:focus-visible,
  .lens-list button:hover,
  .lens-list button:focus-visible {
    border-color: var(--accent-color, #2563eb);
    outline: 2px solid rgba(37, 99, 235, 0.22);
    outline-offset: 2px;
  }

  @media (max-width: 820px) {
    .panel-header,
    .detail-top {
      display: grid;
    }

    .lens-layout,
    .facts {
      grid-template-columns: 1fr;
    }
  }
</style>
