<script>
  import { appState } from '$lib/stores/app.svelte.js';

  /** Command definitions for the palette. */
  const COMMANDS = [
    { id: 'chat', label: 'Go to Chat', icon: 'fas fa-comments', action: () => nav('prompt') },
    { id: 'vault.explore', label: 'Go to Knowledge Vault', icon: 'fas fa-book-atlas', action: () => nav('knowledge-vault') },
    { id: 'vault.rebuild', label: 'Rebuild Knowledge Vault', icon: 'fas fa-rotate', action: () => nav('knowledge-vault') },
    { id: 'refinement.journal', label: 'Go to Refinement Journal', icon: 'fas fa-list-check', action: () => nav('knowledge-vault') },
    { id: 'models', label: 'Go to Models', icon: 'fas fa-microchip', action: () => nav('models') },
    { id: 'training', label: 'Go to Training', icon: 'fas fa-graduation-cap', action: () => nav('training') },
    { id: 'kaizen', label: 'Go to Kaizen', icon: 'fas fa-seedling', action: () => nav('kaizen') },
    { id: 'memory', label: 'Go to Memory', icon: 'fas fa-brain', action: () => nav('memory') },
    { id: 'memory-scopes', label: 'Go to Memory Scopes', icon: 'fas fa-lock', action: () => nav('memory-scopes') },
    { id: 'settings', label: 'Go to Settings', icon: 'fas fa-cog', action: () => nav('settings') },
    { id: 'dashboard', label: 'Go to Dashboard', icon: 'fas fa-chart-line', action: () => nav('dashboard') },
    { id: 'audit-results', label: 'Go to Audit Results', icon: 'fas fa-clipboard-check', action: () => nav('audit-results') },
    { id: 'projects', label: 'Go to Projects', icon: 'fas fa-sitemap', action: () => nav('workflow') },
    { id: 'agents', label: 'Go to Agents', icon: 'fas fa-robot', action: () => nav('agents') },
    { id: 'tasks', label: 'Go to Task Queue', icon: 'fas fa-tasks', action: () => nav('tasks') },
    { id: 'plan', label: 'Go to Plan Builder', icon: 'fas fa-project-diagram', action: () => nav('decomposition') },
    { id: 'workbench-conversation', label: 'Go to Conversation', icon: 'fas fa-comment-dots', action: () => nav('workbench-conversation') },
    { id: 'workbench-shell', label: 'Go to Workbench Shell', icon: 'fas fa-compass-drafting', action: () => nav('workbench-shell') },
    { id: 'workbench-console', label: 'Go to Workbench Console', icon: 'fas fa-table-columns', action: () => nav('workbench-console') },
    { id: 'memory-review-graph', label: 'Go to Memory Review Graph', icon: 'fas fa-diagram-project', action: () => nav('memory-review-graph') },
    { id: 'workbench-query', label: 'Go to Graph Query', icon: 'fas fa-diagram-project', action: () => nav('workbench-query') },
    { id: 'workbench-user-observability', label: 'Go to User Observability', icon: 'fas fa-chart-simple', action: () => nav('workbench-user-observability') },
    { id: 'domain-kits', label: 'Go to Domain Kits', icon: 'fas fa-layer-group', action: () => nav('domain-kits') },
    { id: 'domain-review', label: 'Go to Domain Review', icon: 'fas fa-clipboard-check', action: () => nav('domain-review') },
    { id: 'promotion-inbox', label: 'Go to Promotion Inbox', icon: 'fas fa-inbox', action: () => nav('promotion-inbox') },
    { id: 'evidence-notebooks', label: 'Go to Evidence Notebooks', icon: 'fas fa-book-open', action: () => nav('evidence-notebooks') },
    { id: 'workbench-playground', label: 'Go to Playground', icon: 'fas fa-vials', action: () => nav('workbench-playground') },
    { id: 'preference-cards', label: 'Go to Preference Cards', icon: 'fas fa-id-card', action: () => nav('preference-cards') },
    { id: 'why-panels', label: 'Go to Why Panels', icon: 'fas fa-circle-question', action: () => nav('why-panels') },
    { id: 'mode-lenses', label: 'Go to Mode Lenses', icon: 'fas fa-filter', action: () => nav('mode-lenses') },
    { id: 'creative-roleplay-studio', label: 'Go to Creative Studio', icon: 'fas fa-masks-theater', action: () => nav('creative-roleplay-studio') },
    { id: 'model-quick-choices', label: 'Go to Model Choices', icon: 'fas fa-list-check', action: () => nav('model-quick-choices') },
    { id: 'run-kernel', label: 'Go to Run Kernel', icon: 'fas fa-rotate', action: () => nav('run-kernel') },
    { id: 'program-tier', label: 'Go to Program Tier', icon: 'fas fa-layer-group', action: () => nav('program-tier') },
    { id: 'workbench-migration', label: 'Go to Migration Wizard', icon: 'fas fa-file-import', action: () => nav('workbench-migration') },
    { id: 'managed-agents', label: 'Go to Managed Agents', icon: 'fas fa-user-gear', action: () => nav('managed-agents') },
    { id: 'professional-life-draft', label: 'Go to Professional Life Draft', icon: 'fas fa-briefcase-medical', action: () => navProfessionalLifeDraft() },
    { id: 'artifact-review', label: 'Go to Artifact Review', icon: 'fas fa-clipboard-list', action: () => nav('artifact-review') },
    { id: 'context-enrichment', label: 'Go to Context Enrichment', icon: 'fas fa-file-shield', action: () => nav('context-enrichment') },
    { id: 'tool-output-savings', label: 'Go to Tool Output Savings', icon: 'fas fa-compress', action: () => nav('tool-output-savings') },
    { id: 'workbench-readiness', label: 'Go to Workbench Readiness', icon: 'fas fa-shield-check', action: () => nav('workbench-readiness') },
    { id: 'approval-chain', label: 'Go to Approval Chain', icon: 'fas fa-shield-halved', action: () => nav('approval-chain') },
    { id: 'workbench-channels', label: 'Go to Channel Hub', icon: 'fas fa-tower-broadcast', action: () => nav('workbench-channels') },
    { id: 'workflow-builder', label: 'Go to Workflow Builder', icon: 'fas fa-diagram-next', action: () => nav('workflow-builder') },
    { id: 'command-safety', label: 'Go to Command Safety', icon: 'fas fa-terminal', action: () => nav('command-safety') },
    { id: 'workbench-status', label: 'Go to Workbench Status', icon: 'fas fa-heart-pulse', action: () => nav('workbench-status') },
    { id: 'work-graph', label: 'Go to Work Graph', icon: 'fas fa-diagram-project', action: () => nav('work-graph') },
    { id: 'adaptive-tuning', label: 'Go to Adaptive Tuning', icon: 'fas fa-sliders', action: () => nav('adaptive-tuning') },
    { id: 'workbench-extensions', label: 'Go to Extensions', icon: 'fas fa-puzzle-piece', action: () => nav('workbench-extensions') },
    { id: 'habit-health', label: 'Go to Habit Health', icon: 'fas fa-heart-pulse', action: () => nav('habit-health') },
    { id: 'seriousness-dial', label: 'Go to Seriousness Dial', icon: 'fas fa-sliders', action: () => nav('seriousness-dial') },
    { id: 'promotion-engine', label: 'Go to Promotion Engine', icon: 'fas fa-arrow-up-from-bracket', action: () => nav('promotion-engine') },
    { id: 'approval-diff', label: 'Go to Approval Diff', icon: 'fas fa-code-compare', action: () => nav('approval-diff') },
    { id: 'effective-config', label: 'Go to Effective Config', icon: 'fas fa-sliders', action: () => nav('effective-config') },
    { id: 'resource-cockpit', label: 'Go to Resource Cockpit', icon: 'fas fa-microchip', action: () => nav('resource-cockpit') },
    { id: 'rag-debugger', label: 'Go to RAG Debugger', icon: 'fas fa-magnifying-glass-chart', action: () => nav('rag-debugger') },
    { id: 'mission-control', label: 'Go to Mission Control', icon: 'fas fa-tower-broadcast', action: () => nav('mission-control') },
    { id: 'policy-explainability', label: 'Go to Policy Explainability', icon: 'fas fa-shield-halved', action: () => nav('policy-explainability') },
    { id: 'local-runtime', label: 'Go to Local Runtime', icon: 'fas fa-laptop-code', action: () => nav('local-runtime') },
    { id: 'launcher-settings', label: 'Go to Launcher Settings', icon: 'fas fa-window-restore', action: () => nav('launcher-settings') },
    { id: 'gateway-policy', label: 'Go to Gateway Policy', icon: 'fas fa-shield-alt', action: () => nav('gateway-policy') },
    { id: 'benchmark-importer', label: 'Go to Benchmark Importer', icon: 'fas fa-file-import', action: () => nav('benchmark-importer') },
    { id: 'capabilities', label: 'Go to Capabilities', icon: 'fas fa-layer-group', action: () => nav('capabilities') },
    { id: 'capability-packs', label: 'Go to Capability Packs', icon: 'fas fa-box-open', action: () => nav('capability-packs') },
    { id: 'evidence-assets', label: 'Go to Evidence Assets', icon: 'fas fa-folder-tree', action: () => nav('evidence-assets') },
    { id: 'experiment-lab', label: 'Go to Experiment Lab', icon: 'fas fa-flask', action: () => nav('experiment-lab') },
    { id: 'failure-intelligence', label: 'Go to Failure Intelligence', icon: 'fas fa-triangle-exclamation', action: () => nav('failure-intelligence') },
    { id: 'intake-wizard', label: 'Go to Intake Wizard', icon: 'fas fa-route', action: () => nav('intake-wizard') },
    { id: 'method-library', label: 'Go to Method Library', icon: 'fas fa-book-open-reader', action: () => nav('method-library') },
    { id: 'private-ai-appliance', label: 'Go to Private AI Appliance', icon: 'fas fa-server', action: () => nav('private-ai-appliance') },
    { id: 'repro-capsules', label: 'Go to Repro Capsules', icon: 'fas fa-capsules', action: () => nav('repro-capsules') },
    { id: 'source-tool-cards', label: 'Go to Source Tool Cards', icon: 'fas fa-screwdriver-wrench', action: () => nav('source-tool-cards') },
    { id: 'theme', label: 'Toggle Theme', icon: 'fas fa-palette', action: () => {
      appState.theme = appState.theme === 'dark' ? 'light' : 'dark';
      close();
    }},
  ];

  let query = $state(appState.commandPaletteQuery);
  let selectedIndex = $state(-1);
  let inputEl;
  let panelEl;
  let previousFocus = null;

  let filtered = $derived(
    query.length === 0
      ? COMMANDS
      : COMMANDS.filter((c) => c.label.toLowerCase().includes(query.toLowerCase()))
  );

  // Reset selection whenever the filtered set changes so a stale index never
  // points into an empty array (which would produce NaN via modulo).
  $effect(() => {
    // Access filtered.length to register the dependency.
    void filtered.length;
    selectedIndex = -1;
  });

  function nav(view) {
    appState.currentView = view;
    close();
  }

  function navProfessionalLifeDraft() {
    try {
      window.sessionStorage.setItem('workbench.professionalLifeSurface', 'draft');
    } catch {
      // The route remains reachable even when session storage is unavailable.
    }
    nav('professional-life');
  }

  function close() {
    appState.commandPaletteOpen = false;
    query = '';
    selectedIndex = -1;
    if (previousFocus && typeof previousFocus.focus === 'function') {
      previousFocus.focus();
    }
  }

  function activeOptionId(index) {
    return index >= 0 && filtered[index] ? `command-palette-option-${filtered[index].id}` : undefined;
  }

  function focusableItems() {
    if (!panelEl) return [];
    return Array.from(panelEl.querySelectorAll('button:not([disabled]), input:not([disabled])'));
  }

  function handleKeydown(e) {
    if (e.key === 'Tab') {
      const items = focusableItems();
      if (items.length === 0) return;
      const currentIndex = items.indexOf(document.activeElement);
      const nextIndex = e.shiftKey
        ? currentIndex <= 0 ? items.length - 1 : currentIndex - 1
        : currentIndex === items.length - 1 ? 0 : currentIndex + 1;
      e.preventDefault();
      items[nextIndex].focus();
    } else if (e.key === 'ArrowDown') {
      e.preventDefault();
      // Bail when there are no results — modulo zero produces NaN.
      if (filtered.length === 0) return;
      selectedIndex = selectedIndex < 0 ? 0 : (selectedIndex + 1) % filtered.length;
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      if (filtered.length === 0) return;
      selectedIndex = selectedIndex <= 0 ? filtered.length - 1 : selectedIndex - 1;
    } else if (e.key === 'Enter' && selectedIndex >= 0 && filtered[selectedIndex]) {
      e.preventDefault();
      filtered[selectedIndex].action();
    } else if (e.key === 'Escape') {
      close();
    }
  }

  $effect(() => {
    previousFocus = document.activeElement;
    query = appState.commandPaletteQuery;
    requestAnimationFrame(() => {
      if (inputEl) inputEl.focus();
    });
  });
</script>

<div class="command-palette-overlay">
  <button class="command-palette-backdrop" type="button" aria-label="Close command palette" onclick={close}></button>
  <div
    bind:this={panelEl}
    class="command-palette"
    role="dialog"
    aria-modal="true"
    aria-labelledby="command-palette-title"
    tabindex="-1"
    onkeydown={handleKeydown}
  >
    <h2 id="command-palette-title" class="sr-only">Command palette</h2>
    <div class="command-palette-input-wrap">
      <i class="fas fa-search"></i>
      <input
        bind:this={inputEl}
        bind:value={query}
        type="text"
        class="command-palette-input"
        placeholder="Type a command..."
        aria-label="Command palette search"
        role="combobox"
        aria-expanded="true"
        aria-haspopup="listbox"
        aria-controls="command-palette-results"
        aria-activedescendant={activeOptionId(selectedIndex)}
        onkeydown={handleKeydown}
      />
    </div>

    <div id="command-palette-results" class="command-palette-results" role="listbox" aria-label="Command results">
      {#each filtered as cmd, i}
        <button
          id={`command-palette-option-${cmd.id}`}
          class="command-palette-item"
          class:selected={i === selectedIndex}
          role="option"
          aria-selected={i === selectedIndex}
          onclick={cmd.action}
          onmouseenter={() => { selectedIndex = i; }}
        >
          <i class={cmd.icon}></i>
          <span>{cmd.label}</span>
        </button>
      {/each}

      {#if filtered.length === 0}
        <div class="command-palette-empty" role="status" aria-live="polite">No matching commands</div>
      {/if}
    </div>
  </div>
</div>

<style>
  .command-palette-overlay {
    position: fixed;
    inset: 0;
    display: flex;
    justify-content: center;
    align-items: flex-start;
    padding-top: 15vh;
    z-index: 1000;
    backdrop-filter: blur(4px);
  }

  .command-palette-backdrop {
    position: absolute;
    inset: 0;
    border: 0;
    background: rgba(0, 0, 0, 0.5);
    cursor: default;
  }

  .command-palette {
    position: relative;
    background: var(--surface-elevated, #1a202d);
    border: 1px solid var(--border-default);
    border-radius: 12px;
    width: 560px;
    max-width: 90vw;
    overflow: hidden;
    box-shadow: 0 24px 48px rgba(0, 0, 0, 0.3);
  }

  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
    border: 0;
  }

  .command-palette-input-wrap {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 16px 20px;
    border-bottom: 1px solid var(--border-default);
  }

  .command-palette-input-wrap i {
    color: var(--text-muted);
    font-size: 14px;
  }

  .command-palette-input {
    flex: 1;
    background: transparent;
    border: none;
    outline: 2px solid transparent;
    outline-offset: 2px;
    color: var(--text-primary);
    font-size: 16px;
    font-family: inherit;
  }

  .command-palette-input:focus-visible {
    outline-color: var(--primary);
  }

  .command-palette-results {
    max-height: 400px;
    overflow-y: auto;
    padding: 8px;
  }

  .command-palette-item {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 10px 12px;
    border: none;
    background: transparent;
    color: var(--text-primary);
    font-size: 14px;
    font-family: inherit;
    cursor: pointer;
    border-radius: 8px;
    width: 100%;
    text-align: left;
    transition: background 100ms;
  }

  .command-palette-item.selected,
  .command-palette-item:hover {
    background: var(--glass-bg, rgba(255, 255, 255, 0.05));
  }

  .command-palette-item i {
    color: var(--text-muted);
    width: 20px;
    text-align: center;
  }

  .command-palette-empty {
    padding: 20px;
    text-align: center;
    color: var(--text-muted);
    font-size: 14px;
  }
</style>
