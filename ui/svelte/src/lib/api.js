/**
 * Typed fetch wrappers for all Vetinari REST endpoints.
 *
 * Every function returns the parsed JSON response or throws on HTTP error.
 * Organized by domain: projects, models, training, memory, agents,
 * analytics, plans, rules, credentials, ADRs, skills.
 */

import { nativeKernelPathFromUrl } from './native_kernel_routes.js';

// Native kernel route markers verified by scripts/check_litestar_retirement.py:
// /api/workbench/method-library, /api/v1/workbench/migration.
const API = '/api';
const API_V1 = '/api/v1';

function tauriInvoke() {
  return globalThis.__TAURI__?.core?.invoke ?? globalThis.__TAURI_INTERNALS__?.invoke ?? null;
}

function nativeKernelPath(url) {
  const path = nativeKernelPathFromUrl(String(url));
  return path ? path.split('?')[0] : null;
}

function nativeKernelRejected(path, payload) {
  return path.startsWith('/api/v1/training/')
    || path.startsWith('/api/training/')
    ? payload?.status === 'rejected'
    : false;
}

function jsonBody(options) {
  if (options.body == null || typeof options.body !== 'string') {
    return options.body ?? null;
  }
  return JSON.parse(options.body);
}

export class WorkbenchApiError extends Error {
  constructor(message, { status = null, statusText = '', body = null } = {}) {
    super(message);
    this.name = 'WorkbenchApiError';
    this.status = status;
    this.statusText = statusText;
    this.body = body;
  }
}

function parseErrorBody(rawBody) {
  if (!rawBody) return null;
  try {
    return JSON.parse(rawBody);
  } catch {
    return { detail: rawBody };
  }
}

// -- Internal helpers --------------------------------------------------------

async function request(url, options = {}) {
  // Determine whether this is a mutating method so we can attach the CSRF
  // header required by vetinari/web/csrf.py (CSRFMiddleware, ADR-0071).
  // Browsers cannot add custom headers to cross-origin requests without a
  // CORS preflight, so the presence of X-Requested-With proves same-origin.
  const method = (options.method ?? 'GET').toUpperCase();
  const csrfHeaders =
    method === 'POST' || method === 'PUT' || method === 'DELETE' || method === 'PATCH'
      ? { 'X-Requested-With': 'XMLHttpRequest' }
      : {};
  const invoke = tauriInvoke();
  const kernelPath = nativeKernelPath(url);
  if (invoke && kernelPath) {
    const payload = await invoke('vetinari_kernel_request', {
      payload: {
        method,
        path: String(url),
        body: jsonBody(options),
      },
    });
    if (nativeKernelRejected(kernelPath, payload)) {
      throw new Error(payload.message ?? payload.reason ?? 'native training action rejected');
    }
    return payload;
  }

  const res = await fetch(url, {
    headers: { 'Content-Type': 'application/json', ...csrfHeaders, ...options.headers },
    ...options,
  });
  if (!res.ok) {
    const rawBody = await res.text().catch(() => '');
    const body = parseErrorBody(rawBody);
    throw new WorkbenchApiError(`${res.status} ${res.statusText}: ${rawBody}`, {
      status: res.status,
      statusText: res.statusText,
      body,
    });
  }
  return res.json();
}

export function workbenchKernelRequest(url, options = {}) {
  return request(url, options);
}

function get(url) {
  return request(url);
}

function post(url, body) {
  return request(url, { method: 'POST', body: body != null ? JSON.stringify(body) : undefined });
}

function put(url, body) {
  return request(url, { method: 'PUT', body: JSON.stringify(body) });
}

function del(url) {
  return request(url, { method: 'DELETE' });
}

const WORKBENCH_CHANNEL_SENSITIVE_KEYS = new Set(['api_token', 'api-token', 'apikey', 'api_key', 'token', 'secret', 'password', 'local_path']);

export function sanitizeWorkbenchChannelPayload(value) {
  if (Array.isArray(value)) {
    return value.map((item) => sanitizeWorkbenchChannelPayload(item));
  }
  if (!value || typeof value !== 'object') {
    return value;
  }
  return Object.fromEntries(
    Object.entries(value)
      .filter(([key]) => !WORKBENCH_CHANNEL_SENSITIVE_KEYS.has(String(key).toLowerCase()))
      .map(([key, item]) => [key, sanitizeWorkbenchChannelPayload(item)])
  );
}

// -- Health ------------------------------------------------------------------

export function getHealth() {
  return get('/health');
}

// -- Engine ------------------------------------------------------------------

export function getEngineHealth() {
  return get(`${API_V1}/engine/health`);
}

export function getEngineMetrics() {
  return get(`${API_V1}/engine/metrics`);
}

export function getEngineVersion() {
  return get(`${API_V1}/engine/version`);
}

// -- Full-Spectrum Audit Results --------------------------------------------

export function getFullSpectrumAuditResults({ limit, includeArchived } = {}) {
  const params = new URLSearchParams();
  if (limit !== undefined) params.set('limit', String(limit));
  if (includeArchived) params.set('include_archived', 'true');
  const qs = params.toString();
  return get(`${API}/audit/full-spectrum/results${qs ? `?${qs}` : ''}`);
}

export function getFullSpectrumAuditRun(runId, options = {}) {
  const params = new URLSearchParams();
  if (options.findingLimit !== undefined) params.set('finding_limit', String(options.findingLimit));
  if (options.includeArchived) params.set('include_archived', 'true');
  if (options.findingStatus) params.set('finding_status', String(options.findingStatus));
  if (options.severity) params.set('severity', String(options.severity));
  if (options.lane) params.set('lane', String(options.lane));
  if (options.query) params.set('query', String(options.query));
  const qs = params.toString();
  return get(`${API}/audit/full-spectrum/results/${encodeURIComponent(runId)}${qs ? `?${qs}` : ''}`);
}

export function getProgramTierOverview() {
  return get(`${API}/workbench/program-tier`);
}

export function getProgramTierDetail(programId) {
  return get(`${API}/workbench/program-tier/${encodeURIComponent(programId)}`);
}

// -- Glossary ----------------------------------------------------------------

export function listGlossary() {
  return get(`${API}/glossary`);
}

export function getGlossaryTerm(term) {
  return get(`${API}/glossary/${encodeURIComponent(term)}`);
}

// -- Projects ----------------------------------------------------------------

export function listProjects() {
  // GET /api/projects is read-only; project creation uses the intake/native workflow.
  // Previously called post() which returned 405; corrected to get().
  return get(`${API}/projects`);
}

export function createProject(config) {
  return post(`${API}/new-project`, config);
}

export function getProject(projectId) {
  return get(`${API}/project/${projectId}`);
}

export function sendMessage(projectId, message, attachments = [], options = {}) {
  return post(`${API}/project/${projectId}/message`, { message, attachments, ...options });
}

export function listWorkbenchModeTemplates() {
  return get(`${API}/workbench/mode-templates`);
}

export function getWorkbenchModeTemplate(templateId) {
  return get(`${API}/workbench/mode-templates/${encodeURIComponent(templateId)}`);
}

export function convertChatBranchToArtifact(projectId, conversion) {
  return post(`${API}/workbench/chat-mode/convert`, { project_id: projectId, ...conversion });
}

export function createWorkbenchConversation(payload) {
  return post(`${API}/workbench/conversation`, payload);
}

export function submitIntakeWizard(payload) {
  return post(`${API}/intake/wizard`, payload);
}

export async function uploadAttachment(projectId, file) {
  const form = new FormData();
  form.append('file', file);
  form.append('project_id', projectId);
  const res = await fetch(`${API_V1}/chat/attachments`, {
    method: 'POST',
    // X-Requested-With satisfies CSRFMiddleware (vetinari/web/csrf.py, ADR-0071).
    // Content-Type is intentionally omitted — the browser sets multipart/form-data
    // with the correct boundary automatically when body is FormData.
    headers: { 'X-Requested-With': 'XMLHttpRequest' },
    body: form,
  });
  if (!res.ok) {
    const body = await res.text().catch(() => '');
    throw new Error(`${res.status} ${res.statusText}: ${body}`);
  }
  return res.json();
}

export function getAttachmentUrl(attachmentId) {
  return `${API_V1}/chat/attachments/${attachmentId}`;
}

export function createTask(projectId, task) {
  return post(`${API}/project/${projectId}/task`, task);
}

export function updateTask(projectId, taskId, updates) {
  return put(`${API}/project/${projectId}/task/${taskId}`, updates);
}

export function deleteTask(projectId, taskId) {
  return del(`${API}/project/${projectId}/task/${taskId}`);
}

export function rerunTask(projectId, taskId) {
  return post(`${API}/project/${projectId}/task/${taskId}/rerun`);
}

export function cancelProject(projectId) {
  return post(`${API}/project/${projectId}/cancel`);
}

export function getWorkflowBuilderMetadata() {
  return get(`${API}/workbench/workflow-builder/metadata`);
}

export function validateWorkflowBuilderGraph(graph) {
  return post(`${API}/workbench/workflow-builder/validate`, graph);
}

export function previewWorkflowBuilderGraph(graph) {
  return post(`${API}/workbench/workflow-builder/preview`, graph);
}

export function saveWorkflowBuilderGraph(projectId, graph) {
  return post(`${API}/workbench/workflow-builder/save`, { project_id: projectId, graph });
}

export function listWorkflowBuilderGraphs(projectId) {
  return get(`${API}/workbench/workflow-builder/graphs/${encodeURIComponent(projectId)}`);
}

export function getWorkflowBuilderGraph(projectId, graphId) {
  return get(`${API}/workbench/workflow-builder/graphs/${encodeURIComponent(projectId)}/${encodeURIComponent(graphId)}`);
}

export function getWorkflowBuilderConsole(projectId) {
  return get(`${API}/workbench/workflow-builder/console/${encodeURIComponent(projectId)}`);
}

export function updateWorkflowBuilderSettings(projectId, settings) {
  return post(`${API}/workbench/workflow-builder/settings/${encodeURIComponent(projectId)}`, settings);
}

export function getAdaptiveTuningSnapshot(projectId) {
  return get(`${API}/workbench/adaptive-tuning/snapshot/${encodeURIComponent(projectId)}`);
}

export function normalizeAdaptiveTuningObservation(payload) {
  return post(`${API}/workbench/adaptive-tuning/normalize`, payload);
}

export function proposeAdaptiveTuning(projectId, observations) {
  return post(`${API}/workbench/adaptive-tuning/propose/${encodeURIComponent(projectId)}`, { observations });
}

export function previewAdaptiveTuningProposal(payload) {
  return post(`${API}/workbench/adaptive-tuning/preview`, payload);
}

export function decideAdaptiveTuningHypothesis(projectId, hypothesisId, payload) {
  return post(
    `${API}/workbench/adaptive-tuning/decide/${encodeURIComponent(projectId)}/${encodeURIComponent(hypothesisId)}`,
    payload
  );
}

export function forgetAdaptiveTuningHypothesis(projectId, hypothesisId, payload) {
  return post(
    `${API}/workbench/adaptive-tuning/forget/${encodeURIComponent(projectId)}/${encodeURIComponent(hypothesisId)}`,
    payload
  );
}

export function revokeAdaptiveTuningHypothesis(projectId, hypothesisId, payload) {
  return post(
    `${API}/workbench/adaptive-tuning/revoke/${encodeURIComponent(projectId)}/${encodeURIComponent(hypothesisId)}`,
    payload
  );
}

export function getAdaptiveTuningRollbackReadiness(projectId, proposalId) {
  return get(
    `${API}/workbench/adaptive-tuning/rollback-readiness/${encodeURIComponent(projectId)}/${encodeURIComponent(proposalId)}`
  );
}

export function pauseProject(projectId) {
  return post(`${API}/project/${projectId}/pause`);
}

export function resumeProject(projectId) {
  return post(`${API}/project/${projectId}/resume`);
}

export function getProjectReview(projectId) {
  return get(`${API}/project/${projectId}/review`);
}

export function approveProject(projectId) {
  return post(`${API}/project/${projectId}/approve`);
}

export function archiveProject(projectId) {
  return post(`${API}/project/${projectId}/archive`);
}

export function deleteProject(projectId) {
  return del(`${API}/project/${projectId}`);
}

export function renameProject(projectId, name) {
  return post(`${API}/project/${projectId}/rename`, { name });
}

export function assembleProject(projectId) {
  return post(`${API}/project/${projectId}/assemble`);
}

// -- Models ------------------------------------------------------------------

export function listModels() {
  return get(`${API_V1}/models`);
}

export function refreshModels() {
  return post(`${API_V1}/models/refresh`);
}

export function scoreModels() {
  return post(`${API_V1}/score-models`);
}

export function getModelConfig() {
  return get(`${API_V1}/model-config`);
}

export function saveModelConfig(config) {
  return post(`${API_V1}/model-config`, config);
}

export function swapModel(modelId) {
  return post(`${API_V1}/swap-model`, { model_id: modelId });
}

export function getModelCatalog() {
  return get(`${API_V1}/model-catalog`);
}

export function getModelDetails(modelId) {
  return get(`${API_V1}/models/${modelId}`);
}

export function selectModel(modelId) {
  return post(`${API_V1}/models/select`, { model_id: modelId });
}

export function getModelPolicy() {
  return get(`${API_V1}/models/policy`);
}

export function updateModelPolicy(policy) {
  return put(`${API_V1}/models/policy`, policy);
}

export function reloadModels() {
  return post(`${API_V1}/models/reload`);
}

export function getModelFiles(repoId, { vramGb = 32, useCase = 'general' } = {}) {
  return get(`${API_V1}/models/files?repo_id=${encodeURIComponent(repoId)}&vram_gb=${vramGb}&use_case=${encodeURIComponent(useCase)}`);
}

export function downloadModel(spec) {
  return post(`${API_V1}/models/download`, spec);
}

export function discoverModels() {
  return get(`${API_V1}/discover`);
}

export function getPopularModels() {
  return get(`${API_V1}/models/popular`);
}

export function searchModels(query) {
  return post(`${API_V1}/models/search`, { query });
}

// -- Model Hub (HuggingFace / NGC / Ollama browse and pull) ------------------

/**
 * Search the configured model hub for available repos.
 *
 * Backed by the native Rust kernel `/api/models/hub/search` compatibility route.
 *
 * @param {string} query - Free-text query (repo name fragment, tag, etc.).
 * @returns {Promise<{results: Array<object>}>} Hub search payload.
 */
export function searchModelHub(query) {
  return get(`${API}/models/hub/search?q=${encodeURIComponent(query)}`);
}

/**
 * Request a pull of a hub model to local storage.
 *
 * Backed by the native Rust kernel `/api/models/hub/pull` compatibility route.
 *
 * @param {{repo_id: string, filename?: string, model_format?: string}} spec
 *   Pull specification: ``repo_id`` is required; ``filename`` narrows to a
 *   single artifact and ``model_format`` selects the storage layout.
 * @returns {Promise<object>} Pull-start receipt with status + download_id.
 */
export function pullModelFromHub(spec) {
  return post(`${API}/models/hub/pull`, spec);
}

// -- Chat completions (OpenAI-compatible, FSA-0048) --------------------------

/**
 * Send a chat-completion request via the OpenAI-compatible `/v1/chat/completions` route.
 *
 * Used by the experiment-lab side-by-side comparison (FSA-0048) to run the
 * same prompt against multiple models without going through the project
 * message pipeline.
 *
 * @param {object} payload - OpenAI chat-completions request body
 *   (``model``, ``messages``, optional ``temperature``/``max_tokens``/etc.).
 * @returns {Promise<object>} Parsed JSON response (choices, usage, ...).
 */
export function createChatCompletion(payload) {
  return post('/v1/chat/completions', payload);
}

// -- Extensions (FSA-0057) ---------------------------------------------------

const LOCAL_EXTENSION_ENTRYPOINT_PATTERNS = [
  /^local:/i,
  /^file:/i,
  /^(?:\.{1,2}[\\/]|[a-zA-Z]:[\\/]|[\\/]{1,2})/,
];

function assertMarketplaceExtensionInstall(body = {}) {
  const manifest = body?.manifest;
  const extensionId = String(body?.extension_id ?? manifest?.extension_id ?? manifest?.id ?? manifest?.name ?? '').trim();
  const marketplaceRef = String(
    body?.marketplace_ref
      ?? manifest?.marketplace_ref
      ?? manifest?.registry_ref
      ?? manifest?.source_ref
      ?? '',
  ).trim();
  const entrypoint = String(manifest?.entrypoint ?? '').trim();

  if (!extensionId) {
    throw new Error('extension install requires a marketplace extension id');
  }

  if (manifest && !marketplaceRef) {
    throw new Error('extension manifest installs require marketplace_ref provenance');
  }

  if (entrypoint && LOCAL_EXTENSION_ENTRYPOINT_PATTERNS.some((pattern) => pattern.test(entrypoint))) {
    throw new Error('local extension manifest entrypoints are not installable from the UI');
  }

  return {
    extension_id: extensionId,
    marketplace_ref: marketplaceRef || `marketplace:${extensionId}`,
    manifest,
  };
}

/**
 * List installed workbench extensions.
 *
 * Backed by GET /api/workbench/extensions.
 *
 * @returns {Promise<{extensions: Array<object>}>} Extension list payload.
 */
export function listExtensions() {
  return get(`${API}/workbench/extensions`);
}

/**
 * Install a workbench extension selected from the marketplace.
 *
 * Backed by POST /api/workbench/extensions/import.
 *
 * @param {{extension_id: string, marketplace_ref?: string, manifest?: object}} body
 *   Marketplace install request. Raw local/file manifest entrypoints are rejected
 *   client-side before the native kernel receives a request.
 * @returns {Promise<{extension: object}>} Installed extension descriptor.
 */
export function installExtension(body) {
  const requestBody = assertMarketplaceExtensionInstall(body);
  return post(`${API}/workbench/extensions/import`, requestBody);
}

export const __extensionInstallGuardsForTest = {
  assertMarketplaceExtensionInstall,
};

export function listWorkbenchExtensions() {
  return get(`${API}/workbench/extensions`);
}

export function getWorkbenchExtension(extensionId) {
  return get(`${API}/workbench/extensions/${encodeURIComponent(extensionId)}`);
}

export function selectWorkbenchExtension(extensionId) {
  return post(`${API}/workbench/extensions/${encodeURIComponent(extensionId)}/select`);
}

// -- RAG document upload (FSA-0052) ------------------------------------------

/**
 * Upload one document file into the shared RAG knowledge base.
 *
 * Backed by POST /api/workbench/rag/documents. Sends the file as multipart form data
 * under the field name ``data`` so the backend can stream it without first
 * materializing it as JSON.
 *
 * @param {File|Blob} file - Browser File or Blob to upload.
 * @returns {Promise<{doc_id: string, ingested_chunks: number, source: string}>}
 *   Server-side ingestion receipt.
 */
export async function uploadRagDocument(file) {
  const form = new FormData();
  form.append('data', file);
  const res = await fetch(`${API}/workbench/rag/documents`, {
    method: 'POST',
    headers: { 'X-Requested-With': 'XMLHttpRequest' },
    body: form,
  });
  if (!res.ok) {
    const body = await res.text().catch(() => '');
    throw new Error(`${res.status} ${res.statusText}: ${body}`);
  }
  return res.json();
}

// -- Memory ------------------------------------------------------------------

export function getMemoryEntries(params = {}) {
  const qs = new URLSearchParams(params).toString();
  return get(`${API_V1}/memory${qs ? '?' + qs : ''}`);
}

export function searchMemory(query) {
  return get(`${API_V1}/memory/search?q=${encodeURIComponent(query)}`);
}

export function addMemoryEntry(entry) {
  return post(`${API_V1}/memory`, entry);
}

export function updateMemoryEntry(entryId, updates) {
  return put(`${API_V1}/memory/${entryId}`, updates);
}

export function deleteMemoryEntry(entryId) {
  return del(`${API_V1}/memory/${entryId}`);
}

export function getMemorySessions() {
  return get(`${API_V1}/memory/sessions`);
}

export function getMemoryStats() {
  return get(`${API_V1}/memory/stats`);
}

export function getWorkbenchMemoryReviewGraph(params = {}) {
  const qs = new URLSearchParams(params).toString();
  return get(`/api/workbench/memory/review-graph${qs ? '?' + qs : ''}`);
}

export function getKnowledgeVaultEntries(params = {}) {
  const qs = new URLSearchParams(params).toString();
  return get(`${API}/workbench/knowledge_vault/entries${qs ? '?' + qs : ''}`);
}

export function getKnowledgeVaultRejected() {
  return get(`${API}/workbench/knowledge_vault/rejected`);
}

export function exportKnowledgeVault(requestedScope = 'shareable') {
  return post(`${API}/workbench/knowledge_vault/export`, { requested_scope: requestedScope });
}

export function rebuildKnowledgeVault() {
  return post(`${API}/workbench/knowledge_vault/rebuild`, {});
}

export function enrichWorkbenchContext(payload) {
  return post(`${API}/workbench/context-enrichment/enrich`, payload);
}

export function preflightWorkbenchContextEdit(payload) {
  return post(`${API}/workbench/context-enrichment/edit-preflight`, payload);
}

export async function squashToolOutputPreview(payload) {
  try {
    return await post(`${API}/workbench/tool-output-squasher/preview`, payload);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    const bodyStart = message.indexOf(': ');
    if (message.startsWith('409 ') && bodyStart !== -1) {
      return JSON.parse(message.slice(bodyStart + 2));
    }
    if (message.startsWith('401 ')) {
      const adminError = new Error('Admin access required for Tool Output Savings.');
      adminError.code = 'admin_required';
      throw adminError;
    }
    throw error;
  }
}

export async function fetchWorkbenchReadinessSnapshot(projectId = 'default') {
  try {
    return await get(`${API}/workbench/readiness/snapshot?project_id=${encodeURIComponent(projectId)}`);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    const bodyStart = message.indexOf(': ');
    if ((message.startsWith('409 ') || message.startsWith('202 ')) && bodyStart !== -1) {
      return JSON.parse(message.slice(bodyStart + 2));
    }
    if (message.startsWith('401 ')) {
      const adminError = new Error('Admin access required for Workbench Readiness.');
      adminError.code = 'admin_required';
      throw adminError;
    }
    throw error;
  }
}

export async function previewWorkbenchReadinessAdmission(payload) {
  try {
    return await post(`${API}/workbench/readiness/admission-preview`, payload);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    const bodyStart = message.indexOf(': ');
    if ((message.startsWith('409 ') || message.startsWith('202 ')) && bodyStart !== -1) {
      return JSON.parse(message.slice(bodyStart + 2));
    }
    if (message.startsWith('401 ')) {
      const adminError = new Error('Admin access required for Workbench Readiness.');
      adminError.code = 'admin_required';
      throw adminError;
    }
    throw error;
  }
}

export function resolveWorkbenchApprovalChain(payload) {
  return post(`${API}/workbench/approval-chain/resolve`, payload);
}

export function grantWorkbenchApprovalChainSessionAllow(payload) {
  return post(`${API}/workbench/approval-chain/grant-session-allow`, payload);
}

export function revokeWorkbenchApprovalChainSessionAllow(payload) {
  return post(`${API}/workbench/approval-chain/revoke-session-allow`, payload);
}

export function fetchWorkbenchApprovalChainLastDecision() {
  return get(`${API}/workbench/approval-chain/explain-last`);
}

export function workbenchChannels() {
  return get(`${API}/workbench/channels/config`);
}

export async function deliverWorkbenchChannel(payload) {
  try {
    return await post(`${API}/workbench/channels/deliver`, sanitizeWorkbenchChannelPayload(payload));
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    const bodyStart = message.indexOf(': ');
    if (message.startsWith('409 ') && bodyStart !== -1) {
      return JSON.parse(message.slice(bodyStart + 2));
    }
    throw error;
  }
}

export function fetchWorkbenchCommandSafetyProfiles() {
  return get(`${API}/workbench/command-safety/profiles`);
}

export function classifyWorkbenchCommandSafety(payload) {
  return post(`${API}/workbench/command-safety/classify`, payload);
}

export async function decideWorkbenchCommandSafety(payload) {
  try {
    return await post(`${API}/workbench/command-safety/decide`, payload);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    const bodyStart = message.indexOf(': ');
    if (message.startsWith('409 ') && bodyStart !== -1) {
      return JSON.parse(message.slice(bodyStart + 2));
    }
    throw error;
  }
}

export async function routeWorkbenchChannelCommand(payload) {
  try {
    return await post(`${API}/workbench/channels/commands`, sanitizeWorkbenchChannelPayload(payload));
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    const bodyStart = message.indexOf(': ');
    if (message.startsWith('409 ') && bodyStart !== -1) {
      return JSON.parse(message.slice(bodyStart + 2));
    }
    throw error;
  }
}

export function requestWorkbenchChannelApproval(payload) {
  return post(`${API}/workbench/channels/approvals`, payload);
}

export function fetchWorkbenchChannelActivity() {
  return get(`${API}/workbench/channels/activity`);
}

export function fetchWorkbenchCommandSafetyState(projectId, runId, sessionId, surfaceId) {
  return get(`${API}/workbench/command-safety/state/${encodeURIComponent(projectId)}/${encodeURIComponent(runId)}/${encodeURIComponent(sessionId)}/${encodeURIComponent(surfaceId)}`);
}

export async function fetchWorkbenchStatusSnapshot(projectId = 'default') {
  try {
    return await get(`${API}/workbench/status/snapshot?project_id=${encodeURIComponent(projectId)}`);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    if (message.startsWith('401 ')) {
      const adminError = new Error('Admin access required for Workbench Status.');
      adminError.code = 'admin_required';
      throw adminError;
    }
    throw error;
  }
}

export function fetchWorkbenchStatusDomain(domain, projectId = 'default') {
  return get(`${API}/workbench/status/health/${encodeURIComponent(domain)}?project_id=${encodeURIComponent(projectId)}`);
}

export function fetchWorkbenchStatusAssistantContext(projectId = 'default') {
  return get(`${API}/workbench/status/assistant-context?project_id=${encodeURIComponent(projectId)}`);
}

export async function runWorkbenchStatusAction(payload) {
  try {
    return await post(`${API}/workbench/status/actions`, payload);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    const bodyStart = message.indexOf(': ');
    if (message.startsWith('409 ') && bodyStart !== -1) {
      return JSON.parse(message.slice(bodyStart + 2));
    }
    if (message.startsWith('401 ')) {
      const adminError = new Error('Admin access required for Workbench Status.');
      adminError.code = 'admin_required';
      throw adminError;
    }
    throw error;
  }
}

export async function fetchWorkbenchUpdateReadiness(projectId = 'default', channel = 'stable', currentVersion = '0.0.0-dev') {
  try {
    const qs = new URLSearchParams({
      project_id: projectId,
      channel,
      current_version: currentVersion,
    }).toString();
    return await get(`${API}/workbench/updates/readiness?${qs}`);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    const bodyStart = message.indexOf(': ');
    if (message.startsWith('409 ') && bodyStart !== -1) {
      return JSON.parse(message.slice(bodyStart + 2));
    }
    if (message.startsWith('401 ')) {
      const adminError = new Error('Admin access required for Workbench Updates.');
      adminError.code = 'admin_required';
      throw adminError;
    }
    throw error;
  }
}

export function fetchWorkbenchUpdateChannels() {
  return get(`${API}/workbench/updates/channels`);
}

export async function checkWorkbenchUpdates(payload = {}) {
  try {
    return await post(`${API}/workbench/updates/check`, payload);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    const bodyStart = message.indexOf(': ');
    if (message.startsWith('409 ') && bodyStart !== -1) {
      return JSON.parse(message.slice(bodyStart + 2));
    }
    throw error;
  }
}

export async function skipWorkbenchUpdateVersion(payload) {
  try {
    return await post(`${API}/workbench/updates/skip`, payload);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    const bodyStart = message.indexOf(': ');
    if (message.startsWith('409 ') && bodyStart !== -1) {
      return JSON.parse(message.slice(bodyStart + 2));
    }
    throw error;
  }
}

export async function createWorkbenchUpdateRollbackPlan(payload = {}) {
  try {
    return await post(`${API}/workbench/updates/rollback-plan`, payload);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    const bodyStart = message.indexOf(': ');
    if (message.startsWith('409 ') && bodyStart !== -1) {
      return JSON.parse(message.slice(bodyStart + 2));
    }
    throw error;
  }
}

export async function createWorkbenchUpdateSupportBundle(payload = {}) {
  try {
    return await post(`${API}/workbench/updates/support-bundle`, payload);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    const bodyStart = message.indexOf(': ');
    if (message.startsWith('409 ') && bodyStart !== -1) {
      return JSON.parse(message.slice(bodyStart + 2));
    }
    throw error;
  }
}

export function getMemoryRefinementJournal(params = {}) {
  const qs = new URLSearchParams(params).toString();
  return get(`${API}/workbench/memory_refinement/journal${qs ? '?' + qs : ''}`);
}

export function reverseMemoryRefinementEntry(eventId, reason) {
  return post(`${API}/workbench/memory_refinement/journal/reverse`, { event_id: eventId, reason });
}

// -- Agents ------------------------------------------------------------------

export function getAgentStatus() {
  return get(`${API_V1}/agents/status`);
}

export function initializeAgents() {
  return post(`${API_V1}/agents/initialize`);
}

export function getActiveAgents() {
  return get(`${API_V1}/agents/active`);
}

export function getAgentTasks() {
  return get(`${API_V1}/agents/tasks`);
}

export function getAgentMemory() {
  return get(`${API_V1}/agents/memory`);
}

export function getPendingDecisions() {
  return get(`${API_V1}/decisions/pending`);
}

export function submitDecision(decision) {
  return post(`${API_V1}/decisions`, decision);
}

// -- Training ----------------------------------------------------------------

export function startTraining(config) {
  return post(`${API_V1}/training/start`, config);
}

export function pauseTraining() {
  return post(`${API_V1}/training/pause`);
}

export function stopTraining() {
  return post(`${API_V1}/training/stop`);
}

export function dryRunTraining(config) {
  return post(`${API_V1}/training/dry-run`, config);
}

export function setTrainingRules(rules) {
  return post(`${API_V1}/training/rules`, rules);
}

export function syncTrainingData() {
  return post(`${API_V1}/training/sync-data`);
}

export function generateSyntheticData(config) {
  return post(`${API_V1}/training/generate-synthetic`, config);
}

export function getTrainingStatus() {
  return get(`${API_V1}/training/status`);
}

export function getTrainingHistory() {
  return get(`${API_V1}/training/history`);
}

export function getIdleTrainingStats() {
  return get(`${API_V1}/training/idle-stats`);
}

// -- Plans -------------------------------------------------------------------

export function createPlan(plan) {
  return post(`${API_V1}/plan`, plan);
}

export function listPlans() {
  return get(`${API_V1}/plans`);
}

export function getPlan(planId) {
  return get(`${API_V1}/plans/${planId}`);
}

export function updatePlan(planId, updates) {
  return put(`${API_V1}/plans/${planId}`, updates);
}

export function deletePlan(planId) {
  return del(`${API_V1}/plans/${planId}`);
}

export function startPlan(planId) {
  return post(`${API_V1}/plans/${planId}/start`);
}

export function pausePlan(planId) {
  return post(`${API_V1}/plans/${planId}/pause`);
}

export function resumePlan(planId) {
  return post(`${API_V1}/plans/${planId}/resume`);
}

export function cancelPlan(planId) {
  return post(`${API_V1}/plans/${planId}/cancel`);
}

export function getPlanStatus(planId) {
  return get(`${API_V1}/plans/${planId}/status`);
}

export function getDecompositionTemplates() {
  return get(`${API_V1}/decomposition/templates`);
}

export function getDodDor() {
  return get(`${API_V1}/decomposition/dod-dor`);
}

export function decompose(task) {
  return post(`${API_V1}/decomposition/decompose`, task);
}

export function decomposeWithAgent(task) {
  return post(`${API_V1}/decomposition/decompose-agent`, task);
}

export function getDecompositionKnobs() {
  return get(`${API_V1}/decomposition/knobs`);
}

export function getDecompositionHistory() {
  return get(`${API_V1}/decomposition/history`);
}

// -- Rules -------------------------------------------------------------------

export function getRules() {
  return get(`${API_V1}/rules`);
}

export function getGlobalPrompt() {
  return get(`${API_V1}/rules/global-prompt`);
}

/**
 * Persist a global system prompt that applies to all agents at runtime.
 *
 * Wraps the private post() helper so the shared request() function
 * automatically adds the X-Requested-With: XMLHttpRequest header required by
 * CSRFMiddleware (vetinari/web/csrf.py, ADR-0071). A raw fetch without that
 * header returns 403 and silently discards the save.
 *
 * @param {string} prompt - The global system prompt text to persist.
 * @returns {Promise} Resolves with the server response.
 */
export function saveGlobalPrompt(prompt) {
  return post(`${API_V1}/rules/global-prompt`, { prompt });
}

export function getPreferences() {
  return get(`${API_V1}/preferences`);
}

export function setPreferences(preferences) {
  return put(`${API_V1}/preferences`, preferences);
}

// -- Receipts (Control Center / Attention track) ----------------------------

/**
 * List a project's WorkReceipts with optional filtering and pagination.
 *
 * Wraps GET /api/projects/{project_id}/receipts. The Control Center calls
 * this on initial page load to populate per-project counts before
 * subscribing to the SSE stream.
 *
 * @param {string} projectId - Project identifier.
 * @param {object} [options] - Optional filters.
 * @param {string} [options.kind] - WorkReceiptKind filter (e.g. "worker_task").
 * @param {boolean} [options.awaiting] - True/false to filter by awaiting_user.
 * @param {string} [options.since] - ISO-8601 lower bound on finished_at_utc.
 * @param {number} [options.limit=100] - Page size.
 * @param {number} [options.offset=0] - Page offset.
 * @returns {Promise} Resolves with `{project_id, total, offset, limit, receipts}`.
 */
export function listProjectReceipts(projectId, options = {}) {
  const params = new URLSearchParams();
  if (options.kind != null) params.set('kind', options.kind);
  if (options.awaiting != null) params.set('awaiting', String(options.awaiting));
  if (options.since != null) params.set('since', options.since);
  if (options.limit != null) params.set('limit', String(options.limit));
  if (options.offset != null) params.set('offset', String(options.offset));
  const qs = params.toString();
  return get(`${API}/projects/${encodeURIComponent(projectId)}/receipts${qs ? `?${qs}` : ''}`);
}

/**
 * List awaiting receipts across all projects for the Attention track.
 *
 * Wraps GET /api/attention. Each item carries the structured
 * ``awaiting_reason`` set by the Foreman/Inspector at the time the user
 * block was raised — never synthesised on the client.
 *
 * @returns {Promise} Resolves with `{count, items}`.
 */
export function listAttention() {
  return get(`${API}/attention`);
}

// -- Credentials -------------------------------------------------------------

export function listCredentials() {
  return get(`${API}/admin/credentials`);
}

export function setCredentials(sourceType, creds) {
  return post(`${API}/admin/credentials/${sourceType}`, creds);
}

export function rotateCredentials(sourceType) {
  return post(`${API}/admin/credentials/${sourceType}/rotate`);
}

export function deleteCredentials(sourceType) {
  return del(`${API}/admin/credentials/${sourceType}`);
}

export function getCredentialHealth() {
  return get(`${API}/admin/credentials/health`);
}

// -- ADRs --------------------------------------------------------------------

export function listAdrs() {
  return get(`${API}/adr`);
}

export function getAdr(adrId) {
  return get(`${API}/adr/${adrId}`);
}

export function createAdr(adr) {
  return post(`${API}/adr`, adr);
}

export function updateAdr(adrId, updates) {
  return put(`${API}/adr/${adrId}`, updates);
}

export function deprecateAdr(adrId) {
  return post(`${API}/adr/${adrId}/deprecate`);
}

export function getAdrStatistics() {
  return get(`${API}/adr/statistics`);
}

// -- Analytics ---------------------------------------------------------------

export function getAnalyticsOverview() {
  return get(`${API_V1}/analytics/overview`);
}

export function getAnalyticsAdapters() {
  return get(`${API_V1}/analytics/adapters`);
}

export function getAnalyticsMemory() {
  return get(`${API_V1}/analytics/memory`);
}

export function getAnalyticsPlan() {
  return get(`${API_V1}/analytics/plan`);
}

export function getAnalyticsTraces() {
  return get(`${API_V1}/traces`);
}

export function getAnalyticsCost() {
  return get(`${API_V1}/analytics/cost`);
}

export function getAnalyticsSla() {
  return get(`${API_V1}/analytics/sla`);
}

export function getAnalyticsAnomalies() {
  return get(`${API_V1}/analytics/anomalies`);
}

export function getAnalyticsForecast() {
  return get(`${API_V1}/analytics/forecasts`);
}

export function getAnalyticsAlerts() {
  return get(`${API_V1}/analytics/alerts`);
}

// -- Workbench Shell ---------------------------------------------------------

export function getWorkbenchShellSnapshot(projectId = 'default') {
  return get(`${API}/workbench/shell/snapshot?project_id=${encodeURIComponent(projectId)}`);
}

export function getWorkbenchGraphQuerySnapshot(projectId = 'default') {
  return get(`${API}/workbench/query/snapshot?project_id=${encodeURIComponent(projectId)}`);
}

export function getWorkbenchPreferenceCardsSnapshot(projectId = 'default') {
  return get(`${API}/workbench/preference-cards?project_id=${encodeURIComponent(projectId)}`);
}

export function getDomainReviewQueues(projectId = 'default') {
  return get(`${API}/workbench/domain-review/queues?project_id=${encodeURIComponent(projectId)}`);
}

export function submitDomainReview(projectId = 'default', review) {
  return post(`${API}/workbench/domain-review/submit`, { project_id: projectId, ...review });
}

export function getCreativeRoleplayStudio(projectId = 'default') {
  return get(`${API}/workbench/creative-roleplay/studio?project_id=${encodeURIComponent(projectId)}`);
}

export function getMemoryScopes(projectId = 'default') {
  return get(`${API}/workbench/memory-scopes?project_id=${encodeURIComponent(projectId)}`);
}

export function getPromotionRecipes(projectId = 'default') {
  return get(`${API}/workbench/promotions/recipes?project_id=${encodeURIComponent(projectId)}`);
}

export function getRagQueryDefaults(projectId = 'default') {
  return get(`${API}/workbench/rag/query-defaults?project_id=${encodeURIComponent(projectId)}`);
}

export function getRuntimeUxSnapshot(projectId = 'default') {
  return get(`${API}/workbench/runtime-ux/snapshot?project_id=${encodeURIComponent(projectId)}`);
}

export function getSensitiveWorkflowBinding(projectId = 'default') {
  return get(`${API}/workbench/sensitive-workflow/binding?project_id=${encodeURIComponent(projectId)}`);
}

export function fetchResourceCockpitSnapshot() {
  return get('/api/workbench/resource-cockpit/snapshot');
}

export function fetchResourceCockpitLeases() {
  return get('/api/workbench/resource-cockpit/leases');
}

export function fetchResourceCockpitQueued() {
  return get('/api/workbench/resource-cockpit/queued');
}

export function fetchResourceCockpitSafeActions() {
  return get('/api/workbench/resource-cockpit/safe-actions');
}

export function fetchResourceCockpitMachineProfile() {
  return get('/api/workbench/resource-cockpit/machine-profile');
}

export function fetchResourceCockpitPolicyProposals() {
  return get('/api/workbench/resource-cockpit/policy-proposals');
}

export function postResourceCockpitApprovalDiff(proposalId, payload) {
  return post(`/api/workbench/resource-cockpit/policy-proposals/${proposalId}/approval-diff`, payload);
}

// -- Habit Health ------------------------------------------------------------

export function getHabitHealthSummary(userId) {
  return get(`${API}/workbench/habit-health/summary/${encodeURIComponent(userId)}`);
}

export function createHabitHealthRoutine(payload) {
  return post(`${API}/workbench/habit-health/routines`, payload);
}

export function recordHabitHealthCheckIn(payload) {
  return post(`${API}/workbench/habit-health/check-ins`, payload);
}

export function reviewHabitHealthData(userId) {
  return get(`${API}/workbench/habit-health/review/${encodeURIComponent(userId)}`);
}

export function exportHabitHealthData(payload) {
  return post(`${API}/workbench/habit-health/export`, payload);
}

export function deleteHabitHealthData(userId, reason = 'user-request') {
  return post(`${API}/workbench/habit-health/delete`, { user_id: userId, reason });
}

export function previewHabitHealthDownstreamSignal(payload) {
  return post(`${API}/workbench/habit-health/downstream-preview`, payload);
}

// -- Ponder (Planning Service) -----------------------------------------------

export function getPonderModels() {
  return get(`${API}/ponder/models`);
}

export function getPonderTemplates() {
  return get(`${API}/ponder/templates`);
}

export function getPonderHealth() {
  return get(`${API}/ponder/health`);
}

// -- Workflow pipeline API (GET /api/v1/workflows, POST /api/v1/workflows, etc.) --

export function listPipelines() {
  return get(`${API_V1}/workflows`);
}

export function loadPipeline(pipelineId) {
  return get(`${API_V1}/workflows/${encodeURIComponent(pipelineId)}`);
}

export function savePipeline(pipeline) {
  return post(`${API_V1}/workflows`, pipeline);
}

export function validatePipeline(pipelineId) {
  return post(`${API_V1}/workflows/${encodeURIComponent(pipelineId)}/validate`, {});
}
