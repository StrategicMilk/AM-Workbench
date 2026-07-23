<script>
  import { appState } from '$lib/stores/app.svelte.js';
  import Sidebar from '$components/shell/Sidebar.svelte';
  import Header from '$components/shell/Header.svelte';
  import CommandPalette from '$components/shell/CommandPalette.svelte';
  import Toast from '$components/shell/Toast.svelte';
  import Dashboard from '$views/Dashboard.svelte';
  import ModelsView from '$views/ModelsView.svelte';
  import ChatView from '$views/ChatView.svelte';
  import TrainingView from '$views/TrainingView.svelte';
  import MemoryView from '$views/MemoryView.svelte';
  import SettingsView from '$views/SettingsView.svelte';
  import ProjectsView from '$views/ProjectsView.svelte';
  import AgentsView from '$views/AgentsView.svelte';
  import OutputView from '$views/OutputView.svelte';
  import TasksView from '$views/TasksView.svelte';
  import OnboardingView from '$views/OnboardingView.svelte';
  import AuditResultsView from '$views/AuditResultsView.svelte';
  import ProgramTierView from '$views/ProgramTierView.svelte';
  import PlanBuilderView from '$views/PlanBuilderView.svelte';
  import GatewayPolicyConsole from '$views/GatewayPolicyConsole.svelte';
  import LocalRuntimeSetup from '$views/LocalRuntimeSetup.svelte';
  import MissionControl from '$views/MissionControl.svelte';
  import DomainKitsView from '$views/DomainKitsView.svelte';
  import { DomainReviewQueue } from '$lib/components/workbench/domain-review';
  import EvidenceNotebooksView from '$views/EvidenceNotebooksView.svelte';
  import PolicyExplainabilityView from '$views/PolicyExplainabilityView.svelte';
  import PromotionInbox from '$views/PromotionInbox.svelte';
  import RagDebugger from '$views/RagDebugger.svelte';
  import MemoryReviewGraph from '$lib/components/memory/MemoryReviewGraph.svelte';
  import WorkbenchConsole from '$views/WorkbenchConsole.svelte';
  import WorkbenchPlayground from '$views/WorkbenchPlayground.svelte';
  import WorkbenchShell from '$views/WorkbenchShell.svelte';
  import { MemoryScopesPanel } from '$lib/components/workbench/memory_scopes';
  import { ConversationFrontDoor } from '$lib/components/workbench/conversation';
  import PreferenceCardsPanel from '$lib/components/workbench/preferences/PreferenceCardsPanel.svelte';
  import { GraphQueryExplorer } from '$lib/components/workbench/query';
  import { UserObservabilityPanel } from '$lib/components/workbench/user-observability';
  import WhyPanelsView from '$lib/components/workbench/why/WhyPanelsView.svelte';
  import { ApprovalDiffReview } from '$lib/components/workbench/approval-diff';
  import ResourceCockpitView from '$views/ResourceCockpitView.svelte';
  import { EffectiveConfigExplorer } from '$lib/components/workbench/effective-config';
  import { PromotionEngine } from '$lib/components/workbench/promotions';
  import { RigorDial } from '$lib/components/workbench/rigor';
  import { ModeLensPanel } from '$lib/components/workbench/mode_lenses';
  import { CreativeRoleplayStudio } from '$lib/components/workbench/creative';
  import WorkbenchModelChoicesView from '$views/WorkbenchModelChoicesView.svelte';
  import { VaultExplorer } from '$lib/components/workbench/knowledge_vault';
  import LauncherSettingsView from '$views/LauncherSettingsView.svelte';
  import RunKernelView from '$views/RunKernelView.svelte';
  import WorkbenchMigrationWizardView from '$views/WorkbenchMigrationWizardView.svelte';
  import ManagedAgents from '$views/ManagedAgents.svelte';
  import ProfessionalLifeView from '$views/ProfessionalLifeView.svelte';
  import ArtifactReviewView from '$views/ArtifactReviewView.svelte';
  import ContextEnrichmentView from '$views/ContextEnrichmentView.svelte';
  import ToolOutputSavingsView from '$views/ToolOutputSavingsView.svelte';
  import WorkbenchReadinessView from '$views/WorkbenchReadinessView.svelte';
  import ApprovalChainView from '$views/ApprovalChainView.svelte';
  import WorkbenchChannelsView from '$views/WorkbenchChannelsView.svelte';
  import WorkbenchWorkflowBuilderView from '$views/WorkbenchWorkflowBuilderView.svelte';
  import WorkflowPipelineView from '$views/WorkflowPipelineView.svelte';
  import WorkbenchStatusView from '$views/WorkbenchStatusView.svelte';
  import WorkbenchWorkGraphView from '$views/WorkbenchWorkGraphView.svelte';
  import WorkbenchExtensionsView from '$views/WorkbenchExtensionsView.svelte';
  import WorkbenchHabitHealthView from '$views/WorkbenchHabitHealthView.svelte';
  import WorkbenchAdaptiveTuningView from '$views/WorkbenchAdaptiveTuningView.svelte';
  import BenchmarkImporterView from '$views/BenchmarkImporterView.svelte';
  import CapabilitiesView from '$views/CapabilitiesView.svelte';
  import CapabilityPacksView from '$views/CapabilityPacksView.svelte';
  import EvidenceAssetsView from '$views/EvidenceAssetsView.svelte';
  import ExperimentLabView from '$views/ExperimentLabView.svelte';
  import FailureIntelligenceView from '$views/FailureIntelligenceView.svelte';
  import IntakeWizard from '$views/IntakeWizard.svelte';
  import MethodLibraryView from '$views/MethodLibraryView.svelte';
  import PrivateAIApplianceView from '$views/PrivateAIApplianceView.svelte';
  import ReproCapsulesView from '$views/ReproCapsulesView.svelte';
  import SourceToolCardsView from '$views/SourceToolCardsView.svelte';
  import CommandSafetyPanel from '$lib/components/workbench/command_safety/CommandSafetyPanel.svelte';
  import KaizenView from '$components/workbench/kaizen/KaizenView.svelte';

  /** Valid view names for routing. */
  const VALID_VIEWS = new Set([
    'dashboard', 'prompt', 'models', 'training', 'memory', 'settings', 'audit-results',
    'workflow', 'agents', 'output', 'tasks', 'decomposition',
    'gateway-policy', 'local-runtime', 'mission-control', 'promotion-inbox',
    'domain-kits', 'evidence-notebooks', 'policy-explainability', 'rag-debugger',
    'workbench-shell', 'workbench-console', 'workbench-playground', 'workbench-user-observability',
    'domain-review', 'memory-review-graph', 'workbench-conversation', 'workbench-query',
    'preference-cards', 'why-panels', 'approval-diff', 'resource-cockpit', 'effective-config', 'seriousness-dial',
    'promotion-engine', 'memory-scopes', 'mode-lenses', 'creative-roleplay-studio', 'model-quick-choices',
    'knowledge-vault', 'launcher-settings', 'run-kernel', 'program-tier', 'workbench-migration', 'managed-agents',
    'professional-life', 'artifact-review', 'context-enrichment', 'tool-output-savings',
    'workbench-readiness', 'approval-chain', 'workbench-channels', 'workflow-builder', 'workflow-pipeline', 'command-safety', 'workbench-status', 'work-graph', 'adaptive-tuning', 'workbench-extensions', 'habit-health',
    'benchmark-importer', 'capabilities', 'capability-packs', 'evidence-assets', 'experiment-lab', 'failure-intelligence',
    'intake-wizard', 'method-library', 'private-ai-appliance', 'repro-capsules', 'source-tool-cards',
    'kaizen',
  ]);

  const VIEW_SHORTCUTS = new Map([
    ['1', 'prompt'],
    ['2', 'dashboard'],
    ['3', 'workbench-console'],
    ['4', 'rag-debugger'],
    ['5', 'promotion-inbox'],
    ['6', 'workbench-status'],
  ]);

  /** Handle keyboard shortcuts at the app level. */
  function handleKeydown(e) {
    // Ctrl+B: toggle sidebar
    if (e.ctrlKey && e.key === 'b') {
      e.preventDefault();
      appState.sidebarCollapsed = !appState.sidebarCollapsed;
    }
    // Ctrl+K: toggle command palette
    if (e.ctrlKey && e.key === 'k') {
      e.preventDefault();
      appState.commandPaletteOpen = !appState.commandPaletteOpen;
    }
    // Ctrl+Alt+1..6: jump between the primary expert work surfaces.
    if (e.ctrlKey && e.altKey && VIEW_SHORTCUTS.has(e.key)) {
      e.preventDefault();
      appState.currentView = VIEW_SHORTCUTS.get(e.key);
    }
    // Escape: close overlays
    if (e.key === 'Escape') {
      appState.commandPaletteOpen = false;
    }
  }

  function viewFromProjectPath(pathname) {
    const match = pathname.match(/^\/projects\/([^/]+)\/([^/]+)(?:\/|$)/);
    if (!match) return null;
    const projectId = decodeURIComponent(match[1]);
    const view = normalizeView(decodeURIComponent(match[2]));
    if (!VALID_VIEWS.has(view)) return null;
    return { view, projectId };
  }

  function normalizeView(view) {
    return view === 'workbench_extensions' ? 'workbench-extensions' : view;
  }

  function projectPathFor(view) {
    const pathRoute = viewFromProjectPath(window.location.pathname);
    const projectId = pathRoute?.projectId || appState.currentProjectId;
    if (!projectId) return null;
    return `/projects/${encodeURIComponent(projectId)}/${encodeURIComponent(view)}`;
  }

  let routeInitialized = $state(false);

  /** Sync URL routing on load and popstate. */
  function syncRouteFromLocation() {
    const pathRoute = viewFromProjectPath(window.location.pathname);
    if (pathRoute) {
      appState.currentView = pathRoute.view;
      appState.currentProjectId = pathRoute.projectId;
      return;
    }
    const params = new URLSearchParams(window.location.search);
    const queryView = normalizeView(params.get('view'));
    const queryProjectId = params.get('project_id');
    if (queryProjectId) {
      appState.currentProjectId = queryProjectId;
    }
    if (queryView && VALID_VIEWS.has(queryView)) {
      appState.currentView = queryView;
      return;
    }
    const hash = normalizeView(window.location.hash.slice(1));
    if (hash && VALID_VIEWS.has(hash)) {
      appState.currentView = hash;
    }
  }

  if (typeof window !== 'undefined') {
    syncRouteFromLocation();
    routeInitialized = true;
  }

  $effect(() => {
    const onPopState = () => syncRouteFromLocation();
    window.addEventListener('popstate', onPopState);
    return () => window.removeEventListener('popstate', onPopState);
  });

  // Push hash when view changes
  $effect(() => {
    if (!routeInitialized) return;
    const view = appState.currentView;
    const projectPath = projectPathFor(view);
    const nextUrl = projectPath ? `${projectPath}${window.location.search}` : `${window.location.pathname}${window.location.search}#${view}`;
    if (`${window.location.pathname}${window.location.search}${window.location.hash}` !== nextUrl) {
      window.history.replaceState(null, '', nextUrl);
    }
  });

  // Apply theme class to document
  $effect(() => {
    document.documentElement.setAttribute('data-theme', appState.theme);
  });
</script>

<svelte:window onkeydown={handleKeydown} />

<a href="#main-content" class="skip-link skip-to-content">Skip to main content</a>

<div
  class="app-container"
  class:sidebar-collapsed={appState.sidebarCollapsed}
  aria-label="Vetinari - Local LLM Orchestration System"
>
  <Sidebar />

  <main class="main-content" class:sidebar-collapsed={appState.sidebarCollapsed}>
    <Header />

    <div id="main-content" class="view-container" tabindex="-1">
      {#if appState.currentView === 'onboarding'}
        <OnboardingView />
      {:else if appState.currentView === 'dashboard'}
        <Dashboard />
      {:else if appState.currentView === 'prompt'}
        <ChatView />
      {:else if appState.currentView === 'models'}
        <ModelsView />
      {:else if appState.currentView === 'training'}
        <TrainingView />
      {:else if appState.currentView === 'memory'}
        <MemoryView />
      {:else if appState.currentView === 'settings'}
        <SettingsView />
      {:else if appState.currentView === 'workflow'}
        <ProjectsView />
      {:else if appState.currentView === 'agents'}
        <AgentsView />
      {:else if appState.currentView === 'output'}
        <OutputView />
      {:else if appState.currentView === 'tasks'}
        <TasksView />
      {:else if appState.currentView === 'decomposition'}
        <PlanBuilderView />
      {:else if appState.currentView === 'gateway-policy'}
        <GatewayPolicyConsole />
      {:else if appState.currentView === 'local-runtime'}
        <LocalRuntimeSetup />
      {:else if appState.currentView === 'mission-control'}
        <MissionControl projectId={appState.currentProjectId || 'default'} />
      {:else if appState.currentView === 'domain-kits'}
        <DomainKitsView />
      {:else if appState.currentView === 'domain-review'}
        <DomainReviewQueue projectId={appState.currentProjectId || 'default'} />
      {:else if appState.currentView === 'evidence-notebooks'}
        <EvidenceNotebooksView projectId={appState.currentProjectId || 'default'} />
      {:else if appState.currentView === 'policy-explainability'}
        <PolicyExplainabilityView projectId={appState.currentProjectId || 'default'} />
      {:else if appState.currentView === 'promotion-inbox'}
        <PromotionInbox projectId={appState.currentProjectId || 'default'} />
      {:else if appState.currentView === 'rag-debugger'}
        <RagDebugger />
      {:else if appState.currentView === 'memory-review-graph'}
        <MemoryReviewGraph projectId={appState.currentProjectId || 'default'} />
      {:else if appState.currentView === 'workbench-conversation'}
        <ConversationFrontDoor projectId={appState.currentProjectId} onContinue={() => { appState.currentView = 'prompt'; }} />
      {:else if appState.currentView === 'workbench-shell'}
        <WorkbenchShell projectId={appState.currentProjectId || 'default'} />
      {:else if appState.currentView === 'workbench-console'}
        <WorkbenchConsole projectId={appState.currentProjectId || 'default'} />
      {:else if appState.currentView === 'workbench-playground'}
        <WorkbenchPlayground />
      {:else if appState.currentView === 'workbench-query'}
        <GraphQueryExplorer projectId={appState.currentProjectId || 'default'} />
      {:else if appState.currentView === 'preference-cards'}
        <PreferenceCardsPanel projectId={appState.currentProjectId || 'default'} />
      {:else if appState.currentView === 'workbench-user-observability'}
        <UserObservabilityPanel projectId={appState.currentProjectId || 'default'} />
      {:else if appState.currentView === 'why-panels'}
        <WhyPanelsView projectId={appState.currentProjectId || 'default'} />
      {:else if appState.currentView === 'approval-diff'}
        <ApprovalDiffReview projectId={appState.currentProjectId || 'default'} />
      {:else if appState.currentView === 'resource-cockpit'}
        <ResourceCockpitView />
      {:else if appState.currentView === 'effective-config'}
        <EffectiveConfigExplorer projectId={appState.currentProjectId || 'default'} />
      {:else if appState.currentView === 'seriousness-dial'}
        <RigorDial projectId={appState.currentProjectId || 'default'} />
      {:else if appState.currentView === 'promotion-engine'}
        <PromotionEngine projectId={appState.currentProjectId || 'default'} />
      {:else if appState.currentView === 'memory-scopes'}
        <MemoryScopesPanel projectId={appState.currentProjectId || 'default'} />
      {:else if appState.currentView === 'mode-lenses'}
        <ModeLensPanel projectId={appState.currentProjectId || 'default'} />
      {:else if appState.currentView === 'creative-roleplay-studio'}
        <CreativeRoleplayStudio projectId={appState.currentProjectId || 'default'} />
      {:else if appState.currentView === 'model-quick-choices'}
        <WorkbenchModelChoicesView />
      {:else if appState.currentView === 'knowledge-vault'}
        <VaultExplorer projectId={appState.currentProjectId || 'default'} />
      {:else if appState.currentView === 'launcher-settings'}
        <LauncherSettingsView />
      {:else if appState.currentView === 'run-kernel'}
        <RunKernelView projectId={appState.currentProjectId || 'default'} />
      {:else if appState.currentView === 'program-tier'}
        <ProgramTierView />
      {:else if appState.currentView === 'workbench-migration'}
        <WorkbenchMigrationWizardView projectId={appState.currentProjectId || 'default'} />
      {:else if appState.currentView === 'managed-agents'}
        <ManagedAgents />
      {:else if appState.currentView === 'professional-life'}
        <ProfessionalLifeView projectId={appState.currentProjectId || 'default'} />
      {:else if appState.currentView === 'artifact-review'}
        <ArtifactReviewView projectId={appState.currentProjectId || 'default'} />
      {:else if appState.currentView === 'context-enrichment'}
        <ContextEnrichmentView projectId={appState.currentProjectId || 'default'} />
      {:else if appState.currentView === 'tool-output-savings'}
        <ToolOutputSavingsView projectId={appState.currentProjectId || 'default'} />
      {:else if appState.currentView === 'workbench-readiness'}
        <WorkbenchReadinessView projectId={appState.currentProjectId || 'default'} />
      {:else if appState.currentView === 'approval-chain'}
        <ApprovalChainView projectId={appState.currentProjectId || 'default'} />
      {:else if appState.currentView === 'workbench-channels'}
        <WorkbenchChannelsView projectId={appState.currentProjectId || 'default'} />
      {:else if appState.currentView === 'workflow-builder'}
        <WorkbenchWorkflowBuilderView projectId={appState.currentProjectId || 'default'} />
      {:else if appState.currentView === 'workflow-pipeline'}
        <WorkflowPipelineView />
      {:else if appState.currentView === 'command-safety'}
        <CommandSafetyPanel projectId={appState.currentProjectId || 'default'} />
      {:else if appState.currentView === 'workbench-status'}
        <WorkbenchStatusView projectId={appState.currentProjectId || 'default'} />
      {:else if appState.currentView === 'work-graph'}
        <WorkbenchWorkGraphView projectId={appState.currentProjectId || 'default'} />
      {:else if appState.currentView === 'adaptive-tuning'}
        <WorkbenchAdaptiveTuningView projectId={appState.currentProjectId || 'default'} />
      {:else if appState.currentView === 'workbench-extensions'}
        <WorkbenchExtensionsView />
      {:else if appState.currentView === 'habit-health'}
        <WorkbenchHabitHealthView projectId={appState.currentProjectId || 'default'} />
      {:else if appState.currentView === 'benchmark-importer'}
        <BenchmarkImporterView initialProjectId={appState.currentProjectId || 'default'} />
      {:else if appState.currentView === 'capabilities'}
        <CapabilitiesView />
      {:else if appState.currentView === 'capability-packs'}
        <CapabilityPacksView />
      {:else if appState.currentView === 'evidence-assets'}
        <EvidenceAssetsView projectId={appState.currentProjectId || 'default'} />
      {:else if appState.currentView === 'experiment-lab'}
        <ExperimentLabView projectId={appState.currentProjectId || 'default'} />
      {:else if appState.currentView === 'failure-intelligence'}
        <FailureIntelligenceView projectId={appState.currentProjectId || 'default'} />
      {:else if appState.currentView === 'intake-wizard'}
        <IntakeWizard />
      {:else if appState.currentView === 'method-library'}
        <MethodLibraryView projectId={appState.currentProjectId || 'default'} />
      {:else if appState.currentView === 'private-ai-appliance'}
        <PrivateAIApplianceView />
      {:else if appState.currentView === 'repro-capsules'}
        <ReproCapsulesView projectId={appState.currentProjectId || 'default'} />
      {:else if appState.currentView === 'source-tool-cards'}
        <SourceToolCardsView projectId={appState.currentProjectId || 'default'} />
      {:else if appState.currentView === 'audit-results'}
        <AuditResultsView />
      {:else if appState.currentView === 'kaizen'}
        <KaizenView />
      {:else}
        <ChatView />
      {/if}
    </div>
  </main>
</div>

{#if appState.commandPaletteOpen}
  <CommandPalette />
{/if}

<Toast />
