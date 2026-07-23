export type NavDrawerGroupId =
  | 'primary'
  | 'build'
  | 'operate'
  | 'inspect'
  | 'improve'
  | 'configure'
  | 'experimental';

export type NavDrawerItem = {
  view: string;
  label: string;
  requiresProject: boolean;
  maturity: 'primary' | 'stable' | 'advanced' | 'experimental';
  risk: 'normal' | 'state-changing' | 'admin';
};

export type NavDrawerGroup = {
  id: NavDrawerGroupId;
  label: string;
  items: NavDrawerItem[];
};

export const NAV_DRAWER_GROUPS: NavDrawerGroup[] = [
  {
    id: 'primary',
    label: 'Core workspace',
    items: [
      { view: 'prompt', label: 'Chat', requiresProject: true, maturity: 'primary', risk: 'normal' },
      { view: 'models', label: 'Models', requiresProject: false, maturity: 'primary', risk: 'state-changing' },
      { view: 'training', label: 'Training', requiresProject: true, maturity: 'primary', risk: 'state-changing' },
      { view: 'memory', label: 'Memory', requiresProject: true, maturity: 'primary', risk: 'normal' },
      { view: 'dashboard', label: 'Dashboard', requiresProject: false, maturity: 'primary', risk: 'normal' },
    ],
  },
  {
    id: 'build',
    label: 'Build',
    items: [
      { view: 'workflow', label: 'Projects', requiresProject: false, maturity: 'stable', risk: 'state-changing' },
      { view: 'decomposition', label: 'Plan Builder', requiresProject: true, maturity: 'stable', risk: 'normal' },
      { view: 'workflow-builder', label: 'Workflow Builder', requiresProject: true, maturity: 'advanced', risk: 'state-changing' },
      { view: 'intake-wizard', label: 'Intake Wizard', requiresProject: false, maturity: 'advanced', risk: 'normal' },
      { view: 'method-library', label: 'Method Library', requiresProject: true, maturity: 'advanced', risk: 'normal' },
    ],
  },
  {
    id: 'operate',
    label: 'Operate',
    items: [
      { view: 'mission-control', label: 'Mission Control', requiresProject: true, maturity: 'advanced', risk: 'normal' },
      { view: 'run-kernel', label: 'Run Kernel', requiresProject: true, maturity: 'advanced', risk: 'state-changing' },
      { view: 'capability-packs', label: 'Capability Packs', requiresProject: false, maturity: 'advanced', risk: 'admin' },
      { view: 'workbench-status', label: 'Workbench Status', requiresProject: true, maturity: 'advanced', risk: 'normal' },
      { view: 'resource-cockpit', label: 'Resource Cockpit', requiresProject: false, maturity: 'advanced', risk: 'state-changing' },
    ],
  },
  {
    id: 'inspect',
    label: 'Inspect',
    items: [
      { view: 'evidence-notebooks', label: 'Evidence Notebooks', requiresProject: true, maturity: 'advanced', risk: 'normal' },
      { view: 'evidence-assets', label: 'Evidence Assets', requiresProject: true, maturity: 'advanced', risk: 'normal' },
      { view: 'artifact-review', label: 'Artifact Review', requiresProject: true, maturity: 'advanced', risk: 'normal' },
      { view: 'failure-intelligence', label: 'Failure Intelligence', requiresProject: true, maturity: 'advanced', risk: 'normal' },
      { view: 'repro-capsules', label: 'Repro Capsules', requiresProject: true, maturity: 'advanced', risk: 'normal' },
    ],
  },
  {
    id: 'improve',
    label: 'Improve',
    items: [
      { view: 'promotion-inbox', label: 'Promotion Inbox', requiresProject: true, maturity: 'advanced', risk: 'state-changing' },
      { view: 'promotion-engine', label: 'Promotion Engine', requiresProject: true, maturity: 'advanced', risk: 'state-changing' },
      { view: 'adaptive-tuning', label: 'Adaptive Tuning', requiresProject: true, maturity: 'experimental', risk: 'state-changing' },
      { view: 'experiment-lab', label: 'Experiment Lab', requiresProject: true, maturity: 'experimental', risk: 'state-changing' },
      { view: 'benchmark-importer', label: 'Benchmark Importer', requiresProject: true, maturity: 'advanced', risk: 'state-changing' },
    ],
  },
  {
    id: 'configure',
    label: 'Configure',
    items: [
      { view: 'settings', label: 'Settings', requiresProject: false, maturity: 'stable', risk: 'state-changing' },
      { view: 'local-runtime', label: 'Local Runtime', requiresProject: false, maturity: 'advanced', risk: 'admin' },
      { view: 'launcher-settings', label: 'Launcher Settings', requiresProject: false, maturity: 'advanced', risk: 'admin' },
      { view: 'gateway-policy', label: 'Gateway Policy', requiresProject: true, maturity: 'advanced', risk: 'admin' },
      { view: 'command-safety', label: 'Command Safety', requiresProject: true, maturity: 'advanced', risk: 'admin' },
    ],
  },
  {
    id: 'experimental',
    label: 'Experimental',
    items: [
      { view: 'private-ai-appliance', label: 'Private AI Appliance', requiresProject: false, maturity: 'experimental', risk: 'admin' },
      { view: 'source-tool-cards', label: 'Source Tool Cards', requiresProject: true, maturity: 'experimental', risk: 'normal' },
      { view: 'creative-roleplay-studio', label: 'Creative Studio', requiresProject: true, maturity: 'experimental', risk: 'normal' },
      { view: 'professional-life', label: 'Professional Life', requiresProject: true, maturity: 'experimental', risk: 'normal' },
      { view: 'habit-health', label: 'Habit Health', requiresProject: true, maturity: 'experimental', risk: 'state-changing' },
    ],
  },
];

export function findNavDrawerGroup(view: string): NavDrawerGroup | undefined {
  return NAV_DRAWER_GROUPS.find((group) => group.items.some((item) => item.view === view));
}
