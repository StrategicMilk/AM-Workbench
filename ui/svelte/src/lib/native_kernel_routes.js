export const NATIVE_KERNEL_PREFIXES = [
  '/health',
  '/ready',
  '/mcp/tools',
  '/api/v1/kernel/status',
  '/api/v1/engine',
  '/api/audit',
  '/api/intake',
  '/api/training',
  '/api/v1/training',
  '/api/v1/workflows',
  '/api/v1/autonomy',
  '/api/models',
  '/api/workbench/approval-chain',
  '/api/workbench/artifact-reviews',
  '/api/workbench/chat-mode',
  '/api/workbench/command-safety',
  '/api/workbench/console',
  '/api/workbench/context-enrichment',
  '/api/workbench/conversation',
  '/api/workbench/domain-review',
  '/api/workbench/evidence-assets',
  '/api/workbench/evidence-notebooks',
  '/api/workbench/experiment-lab',
  '/api/workbench/method-library',
  '/api/workbench/adaptive-tuning',
  '/api/workbench/resource-cockpit',
  '/api/workbench/capability-packs',
  '/api/workbench/domain-kits',
  '/api/workbench/workflow-builder',
  '/api/workbench/channels',
  '/api/workbench/benchmark',
  '/api/workbench/extensions',
  '/api/v1/workbench/annotation',
  '/api/v1/workbench/launcher',
  '/api/v1/workbench/migration',
  '/api/v1/workbench/onboarding',
  '/api/workbench/habit-health',
  '/api/workbench/knowledge_vault',
  '/api/workbench/managed-agents',
  '/api/workbench/memory',
  '/api/workbench/memory_refinement',
  '/api/workbench/mode-templates',
  '/api/workbench/model-choices',
  '/api/workbench/playground',
  '/api/workbench/policy-explainability',
  '/api/workbench/preference-cards',
  '/api/workbench/private-ai',
  '/api/workbench/prompt-engineering',
  '/api/workbench/query',
  '/api/workbench/rag',
  '/api/workbench/readiness',
  '/api/workbench/repro-capsules',
  '/api/workbench/run-kernel',
  '/api/workbench/shell',
  '/api/workbench/source-cards',
  '/api/workbench/status',
  '/api/workbench/tool-cards',
  '/api/workbench/tool-guides',
  '/api/workbench/tool-output-squasher',
  '/api/workbench/updates',
  '/api/workbench/work-graph',
];

const PROJECT_NATIVE_ROUTE = /^\/api\/v1\/projects\/[^/]+\/(?:mission-control|workbench)(?:\/|$)/;
const V1_WORKBENCH_GATEWAY_POLICY_ROUTE = /^\/api\/v1\/workbench\/[^/]+\/gateway-policy(?:\/|$)/;

export function nativeProjectStreamPath(projectId) {
  return `/api/v1/projects/${encodeURIComponent(projectId)}/workbench/stream`;
}

export function nativeKernelPathFromUrl(rawUrl, origin = globalThis.window?.location?.origin ?? 'http://localhost') {
  const url = new URL(rawUrl, origin);
  if (url.origin !== origin) return null;
  const ownedByKernel =
    NATIVE_KERNEL_PREFIXES.some((prefix) => url.pathname === prefix || url.pathname.startsWith(`${prefix}/`))
    || PROJECT_NATIVE_ROUTE.test(url.pathname)
    || V1_WORKBENCH_GATEWAY_POLICY_ROUTE.test(url.pathname);
  return ownedByKernel ? `${url.pathname}${url.search}` : null;
}
