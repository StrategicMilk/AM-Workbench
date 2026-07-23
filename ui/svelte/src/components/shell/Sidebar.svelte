<script>
  import { appState } from '$lib/stores/app.svelte.js';
  import * as api from '$lib/api.js';
  import { relativeTime } from '$lib/utils/format.js';

  /**
   * Navigation items for the primary sidebar menu.
   * icon: Font Awesome class, view: key in VIEW_MAP, label: display text.
   */
  const NAV_ITEMS = [
    { icon: 'fas fa-comments', view: 'prompt', label: 'Chat' },
    { icon: 'fas fa-microchip', view: 'models', label: 'Models' },
    { icon: 'fas fa-graduation-cap', view: 'training', label: 'Training' },
    { icon: 'fas fa-seedling', view: 'kaizen', label: 'Kaizen' },
    { icon: 'fas fa-brain', view: 'memory', label: 'Memory' },
    { icon: 'fas fa-book-atlas', view: 'knowledge-vault', label: 'Knowledge Vault' },
    { icon: 'fas fa-lock', view: 'memory-scopes', label: 'Memory Scopes' },
    { icon: 'fas fa-cog', view: 'settings', label: 'Settings' },
    { icon: 'fas fa-chart-line', view: 'dashboard', label: 'Dashboard' },
    { icon: 'fas fa-clipboard-check', view: 'audit-results', label: 'Audit Results' },
  ];

  /** Advanced tools (collapsible section). */
  const ADVANCED_ITEMS = [
    { icon: 'fas fa-sitemap', view: 'workflow', label: 'Projects' },
    { icon: 'fas fa-robot', view: 'agents', label: 'Agents' },
    { icon: 'fas fa-file-code', view: 'output', label: 'Output Viewer' },
    { icon: 'fas fa-tasks', view: 'tasks', label: 'Task Queue' },
    { icon: 'fas fa-project-diagram', view: 'decomposition', label: 'Plan Builder' },
    { icon: 'fas fa-comment-dots', view: 'workbench-conversation', label: 'Conversation' },
    { icon: 'fas fa-compass-drafting', view: 'workbench-shell', label: 'Workbench Shell' },
    { icon: 'fas fa-table-columns', view: 'workbench-console', label: 'Workbench Console' },
    { icon: 'fas fa-network-wired', view: 'memory-review-graph', label: 'Memory Review Graph' },
    { icon: 'fas fa-share-nodes', view: 'workbench-query', label: 'Graph Query' },
    { icon: 'fas fa-chart-simple', view: 'workbench-user-observability', label: 'User Observability' },
    { icon: 'fas fa-layer-group', view: 'domain-kits', label: 'Domain Kits' },
    { icon: 'fas fa-clipboard-check', view: 'domain-review', label: 'Domain Review' },
    { icon: 'fas fa-inbox', view: 'promotion-inbox', label: 'Promotion Inbox' },
    { icon: 'fas fa-book-open', view: 'evidence-notebooks', label: 'Evidence Notebooks' },
    { icon: 'fas fa-vials', view: 'workbench-playground', label: 'Playground' },
    { icon: 'fas fa-id-card', view: 'preference-cards', label: 'Preference Cards' },
    { icon: 'fas fa-circle-question', view: 'why-panels', label: 'Why Panels' },
    { icon: 'fas fa-filter', view: 'mode-lenses', label: 'Mode Lenses' },
    { icon: 'fas fa-masks-theater', view: 'creative-roleplay-studio', label: 'Creative Studio' },
    { icon: 'fas fa-list-check', view: 'model-quick-choices', label: 'Model Choices' },
    { icon: 'fas fa-rotate', view: 'run-kernel', label: 'Run Kernel' },
    { icon: 'fas fa-diagram-predecessor', view: 'program-tier', label: 'Program Tier' },
    { icon: 'fas fa-file-import', view: 'workbench-migration', label: 'Migration Wizard' },
    { icon: 'fas fa-user-gear', view: 'managed-agents', label: 'Managed Agents' },
    { icon: 'fas fa-briefcase-medical', view: 'professional-life', label: 'Professional Life' },
    { icon: 'fas fa-clipboard-list', view: 'artifact-review', label: 'Artifact Review' },
    { icon: 'fas fa-file-shield', view: 'context-enrichment', label: 'Context Enrichment' },
    { icon: 'fas fa-compress', view: 'tool-output-savings', label: 'Tool Output Savings' },
    { icon: 'fas fa-shield-check', view: 'workbench-readiness', label: 'Workbench Readiness' },
    { icon: 'fas fa-shield-halved', view: 'approval-chain', label: 'Approval Chain' },
    { icon: 'fas fa-tower-broadcast', view: 'workbench-channels', label: 'Channel Hub' },
    { icon: 'fas fa-diagram-next', view: 'workflow-builder', label: 'Workflow Builder' },
    { icon: 'fas fa-terminal', view: 'command-safety', label: 'Command Safety' },
    { icon: 'fas fa-heart-pulse', view: 'workbench-status', label: 'Workbench Status' },
    { icon: 'fas fa-diagram-project', view: 'work-graph', label: 'Work Graph' },
    { icon: 'fas fa-sliders', view: 'adaptive-tuning', label: 'Adaptive Tuning' },
    { icon: 'fas fa-puzzle-piece', view: 'workbench-extensions', label: 'Extensions' },
    { icon: 'fas fa-seedling', view: 'habit-health', label: 'Habit Health' },
    { icon: 'fas fa-gauge-high', view: 'seriousness-dial', label: 'Seriousness Dial' },
    { icon: 'fas fa-arrow-up-from-bracket', view: 'promotion-engine', label: 'Promotion Engine' },
    { icon: 'fas fa-code-compare', view: 'approval-diff', label: 'Approval Diff' },
    { icon: 'fas fa-gauge', view: 'resource-cockpit', label: 'Resource Cockpit' },
    { icon: 'fas fa-magnifying-glass-chart', view: 'rag-debugger', label: 'RAG Debugger' },
    { icon: 'fas fa-satellite-dish', view: 'mission-control', label: 'Mission Control' },
    { icon: 'fas fa-scale-balanced', view: 'policy-explainability', label: 'Policy Explainability' },
    { icon: 'fas fa-laptop-code', view: 'local-runtime', label: 'Local Runtime' },
    { icon: 'fas fa-window-restore', view: 'launcher-settings', label: 'Launcher Settings' },
    { icon: 'fas fa-shield-alt', view: 'gateway-policy', label: 'Gateway Policy' },
    { icon: 'fas fa-cloud-arrow-down', view: 'benchmark-importer', label: 'Benchmark Importer' },
    { icon: 'fas fa-boxes-stacked', view: 'capabilities', label: 'Capabilities' },
    { icon: 'fas fa-box-open', view: 'capability-packs', label: 'Capability Packs' },
    { icon: 'fas fa-folder-tree', view: 'evidence-assets', label: 'Evidence Assets' },
    { icon: 'fas fa-flask', view: 'experiment-lab', label: 'Experiment Lab' },
    { icon: 'fas fa-triangle-exclamation', view: 'failure-intelligence', label: 'Failure Intelligence' },
    { icon: 'fas fa-route', view: 'intake-wizard', label: 'Intake Wizard' },
    { icon: 'fas fa-book-open-reader', view: 'method-library', label: 'Method Library' },
    { icon: 'fas fa-server', view: 'private-ai-appliance', label: 'Private AI Appliance' },
    { icon: 'fas fa-capsules', view: 'repro-capsules', label: 'Repro Capsules' },
    { icon: 'fas fa-screwdriver-wrench', view: 'source-tool-cards', label: 'Source Tool Cards' },
  ];

  const ADVANCED_GROUP_RULES = [
    {
      label: 'Operate',
      views: new Set([
        'workbench-shell', 'workbench-console', 'workbench-status', 'mission-control',
        'tasks', 'workflow', 'agents', 'run-kernel', 'program-tier', 'command-safety', 'local-runtime',
        'launcher-settings', 'resource-cockpit',
      ]),
    },
    {
      label: 'Improve',
      views: new Set([
        'promotion-inbox', 'promotion-engine', 'approval-chain', 'approval-diff',
        'adaptive-tuning', 'method-library', 'experiment-lab', 'rag-debugger',
        'benchmark-importer', 'capabilities', 'capability-packs',
      ]),
    },
    {
      label: 'Evidence',
      views: new Set([
        'work-graph', 'evidence-assets', 'evidence-notebooks', 'memory-review-graph',
        'workbench-query', 'context-enrichment', 'repro-capsules', 'source-tool-cards',
        'failure-intelligence', 'policy-explainability', 'workbench-readiness',
      ]),
    },
    {
      label: 'Build',
      views: new Set([
        'decomposition', 'workflow-builder', 'domain-kits', 'domain-review',
        'artifact-review', 'workbench-channels', 'workbench-extensions',
        'workbench-migration', 'managed-agents', 'gateway-policy',
      ]),
    },
    {
      label: 'User Modes',
      views: new Set([
        'workbench-conversation', 'workbench-playground', 'workbench-user-observability',
        'preference-cards', 'why-panels', 'mode-lenses', 'creative-roleplay-studio',
        'professional-life', 'habit-health', 'seriousness-dial', 'private-ai-appliance',
        'model-quick-choices', 'tool-output-savings', 'knowledge-vault', 'intake-wizard',
      ]),
    },
  ];

  let advancedOpen = $state(false);
  let advancedQuery = $state('');
  let projects = $state([]);

  function groupForAdvancedItem(item) {
    return ADVANCED_GROUP_RULES.find((group) => group.views.has(item.view))?.label ?? 'Other';
  }

  let filteredAdvancedItems = $derived.by(() => {
    const query = advancedQuery.trim().toLowerCase();
    return ADVANCED_ITEMS
      .map((item) => ({ ...item, group: groupForAdvancedItem(item) }))
      .filter((item) => !query || item.label.toLowerCase().includes(query) || item.group.toLowerCase().includes(query))
      .sort((a, b) => {
        const groupDelta = a.group.localeCompare(b.group);
        return groupDelta || a.label.localeCompare(b.label);
      });
  });

  function startsAdvancedGroup(index) {
    return index === 0 || filteredAdvancedItems[index - 1]?.group !== filteredAdvancedItems[index]?.group;
  }

  function switchView(view) {
    appState.currentView = view;
  }

  function toggleAdvanced() {
    advancedOpen = !advancedOpen;
  }

  async function loadProjects() {
    try {
      const data = await api.listProjects();
      projects = Array.isArray(data) ? data : data?.projects ?? [];
    } catch {
      projects = [];
    }
  }

  $effect(() => {
    loadProjects();
  });
</script>

<aside
  id="main-sidebar"
  class="sidebar"
  class:collapsed={appState.sidebarCollapsed}
  class:open={!appState.sidebarCollapsed}
  role="navigation"
  aria-label="Main navigation"
>
  <div class="logo">
    <i class="fas fa-brain" aria-hidden="true"></i>
    {#if !appState.sidebarCollapsed}
      <span>Vetinari</span>
    {/if}
  </div>

  <nav class="nav-menu" aria-label="Main menu">
    <ul class="nav-list" role="list">
      {#each NAV_ITEMS as item}
        <li role="listitem">
          <button
            class="nav-item"
            class:active={appState.currentView === item.view}
            onclick={() => switchView(item.view)}
            aria-label={item.label}
            aria-current={appState.currentView === item.view ? 'page' : undefined}
            title={appState.sidebarCollapsed ? item.label : undefined}
          >
            <i class={item.icon} aria-hidden="true"></i>
            {#if !appState.sidebarCollapsed}
              <span>{item.label}</span>
            {/if}
          </button>
        </li>
      {/each}
    </ul>

    <!-- Advanced tools divider -->
    <div class="nav-section-divider">
      <button
        class="nav-section-toggle"
        onclick={toggleAdvanced}
        aria-expanded={advancedOpen}
        aria-label={advancedOpen ? 'Collapse advanced tools' : 'Expand advanced tools'}
        title="Toggle advanced tools"
      >
        <i class="fas" class:fa-chevron-right={!advancedOpen} class:fa-chevron-down={advancedOpen} aria-hidden="true"></i>
        {#if !appState.sidebarCollapsed}
          <span>Advanced Tools</span>
        {/if}
      </button>
    </div>

    {#if advancedOpen}
      {#if !appState.sidebarCollapsed}
        <label class="advanced-filter">
          <span class="sr-only">Filter advanced tools</span>
          <i class="fas fa-search" aria-hidden="true"></i>
          <input bind:value={advancedQuery} type="search" placeholder="Filter tools" />
        </label>
      {/if}
      <ul class="nav-advanced nav-list" role="list">
        {#each filteredAdvancedItems as item, i}
          {#if !appState.sidebarCollapsed && startsAdvancedGroup(i)}
            <li class="nav-group-label" aria-hidden="true">{item.group}</li>
          {/if}
          <li role="listitem">
            <button
              class="nav-item nav-item-advanced"
              class:active={appState.currentView === item.view}
              onclick={() => switchView(item.view)}
              aria-label={item.label}
              aria-current={appState.currentView === item.view ? 'page' : undefined}
              title={appState.sidebarCollapsed ? item.label : undefined}
            >
              <i class={item.icon} aria-hidden="true"></i>
              {#if !appState.sidebarCollapsed}
                <span>{item.label}</span>
              {/if}
            </button>
          </li>
        {/each}
      </ul>
    {/if}
  </nav>

  <!-- Project list -->
  {#if !appState.sidebarCollapsed && projects.length > 0}
    <div class="projects-panel">
      <h3 class="projects-panel-title">Projects</h3>
      <div class="project-list">
        {#each projects.slice(0, 10) as project}
          <button
            class="project-card"
            class:active={appState.currentProjectId === project.id}
            aria-label={`Open project ${project.name || project.id}, created ${relativeTime(project.created_at)}`}
            aria-current={appState.currentProjectId === project.id ? 'page' : undefined}
            onclick={() => {
              appState.currentProjectId = project.id;
              appState.currentView = 'prompt';
            }}
          >
            <span class="project-name">{project.name || project.id}</span>
            <span class="project-meta">{relativeTime(project.created_at)}</span>
          </button>
        {/each}
      </div>
    </div>
  {/if}

  <div class="sidebar-footer">
    <div class="status-indicator" class:online={appState.serverConnected}>
      <span class="status-dot" role="img" aria-label={appState.serverConnected ? 'Stream active' : 'No stream'}></span>
      {#if !appState.sidebarCollapsed}
        <span>{appState.serverConnected ? 'Stream active' : 'No stream'}</span>
      {/if}
    </div>
  </div>
</aside>
