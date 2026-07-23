<script>
  import * as api from '$lib/api.js';

  let { projectId = 'default', rigorState = null, onRigorChange = null } = $props();

  const levels = [
    { level: 'just_talk', label: 'Just Talk', pressure: 0, evidence: 'Hidden unless needed' },
    { level: 'help_me_think', label: 'Help Me Think', pressure: 1, evidence: 'Concise summary' },
    { level: 'make_something', label: 'Make Something', pressure: 2, evidence: 'Changed artifacts and checks' },
    { level: 'check_it_carefully', label: 'Check It Carefully', pressure: 3, evidence: 'Show key evidence' },
    { level: 'make_it_reusable', label: 'Make It Reusable', pressure: 4, evidence: 'Full verification summary' },
  ];

  let selected = $state('make_something');
  let persistenceState = $state('loading');
  let active = $derived(levels.find((item) => item.level === selected) ?? levels[2]);

  function storageKey() {
    return `vetinari:rigor-dial:${projectId || 'default'}`;
  }

  function persistedPayload(level) {
    return {
      project_id: projectId || 'default',
      level,
      pressure: levels.find((item) => item.level === level)?.pressure ?? 2,
      persisted_at: new Date().toISOString(),
      receipt_id: `rigor-dial:${projectId || 'default'}:${level}`,
    };
  }

  function cacheLevel(level, payload = persistedPayload(level)) {
    if (typeof window !== 'undefined') {
      window.localStorage.setItem(storageKey(), JSON.stringify(payload));
    }
  }

  function loadCachedLevel() {
    try {
      if (typeof window === 'undefined') return null;
      const raw = window.localStorage.getItem(storageKey());
      if (!raw) return null;
      const payload = JSON.parse(raw);
      return levels.some((item) => item.level === payload?.level) ? payload.level : null;
    } catch {
      return null;
    }
  }

  async function loadPersistedLevel() {
    persistenceState = 'loading';
    try {
      const response = await api.getPreferences();
      const level = response.preferences?.rigorLevel ?? response.preferences?.rigorState?.level;
      if (levels.some((item) => item.level === level)) {
        selected = level;
        persistenceState = 'server';
        return;
      }
      const cached = loadCachedLevel();
      if (cached) {
        selected = cached;
        persistenceState = 'cached';
        return;
      }
      persistenceState = 'default';
    } catch (error) {
      const cached = loadCachedLevel();
      if (cached) {
        selected = cached;
        persistenceState = 'cached';
      } else {
        persistenceState = `unavailable:${error?.message ?? 'unknown'}`;
      }
    }
  }

  async function persistLevel(level) {
    const payload = {
      ...persistedPayload(level),
      source: 'rigor-dial',
    };
    try {
      await api.setPreferences({ rigorLevel: level, rigorState: payload });
      cacheLevel(level, payload);
      persistenceState = 'server';
      onRigorChange?.(payload);
    } catch (error) {
      try {
        cacheLevel(level, payload);
        persistenceState = `cached:${error?.message ?? 'server unavailable'}`;
        onRigorChange?.(payload);
      } catch (cacheError) {
        persistenceState = `blocked:${cacheError?.message ?? error?.message ?? 'unknown'}`;
      }
    }
  }

  $effect(() => {
    if (levels.some((item) => item.level === rigorState?.level)) {
      selected = rigorState.level;
      persistenceState = 'external';
    }
  });

  $effect(() => {
    if (!rigorState?.level) {
      void loadPersistedLevel();
    }
  });
</script>

<section class="rigor-dial" aria-labelledby="rigor-title" data-project-id={projectId}>
  <header class="rigor-header">
    <div>
      <h1 id="rigor-title">Seriousness Dial</h1>
      <p>Set how much verification, evidence, memory, and artifact discipline this project should use.</p>
    </div>
    <div class="active-level" aria-live="polite">
      <span>{active.label}</span>
      <strong>{active.pressure}/4</strong>
      <small>{persistenceState}</small>
    </div>
  </header>

  <div class="level-strip" role="radiogroup" aria-label="Rigor level">
    {#each levels as item}
      <button
        type="button"
        class:active={selected === item.level}
        role="radio"
        aria-checked={selected === item.level}
        aria-describedby={`rigor-evidence-${item.level}`}
        onclick={() => {
          selected = item.level;
          persistLevel(item.level);
        }}
      >
        <span>{item.label}</span>
        <small id={`rigor-evidence-${item.level}`}>{item.evidence}</small>
      </button>
    {/each}
  </div>

  <div class="policy-grid">
    <div>
      <span>Clarification</span>
      <strong>{active.pressure === 0 ? 'Minimal' : active.pressure === 4 ? 'Strict' : 'Balanced'}</strong>
    </div>
    <div>
      <span>Citations</span>
      <strong>{active.pressure >= 3 ? 'Required' : 'When useful'}</strong>
    </div>
    <div>
      <span>Memory</span>
      <strong>{active.pressure >= 2 ? 'Project scoped' : 'Lightweight'}</strong>
    </div>
    <div>
      <span>Evidence</span>
      <strong>{active.evidence}</strong>
    </div>
  </div>
</section>

<style>
  .rigor-dial {
    display: flex;
    flex-direction: column;
    gap: 20px;
    padding: 24px;
    color: var(--text-primary, #111827);
  }

  .rigor-header {
    display: flex;
    justify-content: space-between;
    gap: 18px;
    align-items: flex-start;
  }

  h1 {
    margin: 0 0 8px;
    font-size: 28px;
    line-height: 1.15;
  }

  p {
    margin: 0;
    max-width: 760px;
    color: var(--text-secondary, #4b5563);
  }

  .active-level {
    display: grid;
    gap: 4px;
    min-width: 148px;
    padding: 12px;
    border: 1px solid var(--border-color, #d1d5db);
    border-radius: 8px;
    background: var(--surface-secondary, #f9fafb);
  }

  .active-level strong {
    font-size: 24px;
  }

  .level-strip {
    display: grid;
    grid-template-columns: repeat(5, minmax(120px, 1fr));
    gap: 8px;
  }

  .level-strip button {
    min-height: 44px;
    border: 1px solid var(--border-color, #d1d5db);
    border-radius: 8px;
    background: var(--surface-primary, #fff);
    color: inherit;
    font: inherit;
    cursor: pointer;
  }

  .level-strip button.active {
    border-color: var(--accent-color, #2563eb);
    background: var(--accent-subtle, #eff6ff);
  }

  .policy-grid {
    display: grid;
    grid-template-columns: repeat(4, minmax(150px, 1fr));
    gap: 12px;
  }

  .policy-grid div {
    display: grid;
    gap: 6px;
    padding: 14px;
    border: 1px solid var(--border-color, #d1d5db);
    border-radius: 8px;
    background: var(--surface-primary, #fff);
  }

  .policy-grid span {
    color: var(--text-secondary, #4b5563);
    font-size: 13px;
  }

  @media (max-width: 900px) {
    .rigor-header,
    .policy-grid {
      grid-template-columns: 1fr;
    }

    .rigor-header {
      display: grid;
    }

    .level-strip {
      grid-template-columns: 1fr;
    }
  }
</style>
