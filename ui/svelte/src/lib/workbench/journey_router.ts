export type WorkbenchJourneyView = 'workbench-console' | 'workbench-playground' | 'promotion-inbox';

export type WorkbenchJourneyTarget =
  | 'console'
  | 'playground'
  | 'promotionInbox';

export type WorkbenchJourneyParams = {
  projectId?: string;
  runId?: string;
  traceId?: string;
  evidenceTraceId?: string;
  assetId?: string;
  assetRevision?: string;
  experimentId?: string;
  proposalId?: string;
  promptText?: string;
  score?: number | string;
};

export type WorkbenchJourneyState = Required<Pick<WorkbenchJourneyParams, 'projectId'>> &
  Omit<WorkbenchJourneyParams, 'projectId'>;

const VIEW_BY_TARGET: Record<WorkbenchJourneyTarget, WorkbenchJourneyView> = {
  console: 'workbench-console',
  playground: 'workbench-playground',
  promotionInbox: 'promotion-inbox',
};

const QUERY_KEYS: Record<Exclude<keyof WorkbenchJourneyParams, 'projectId'>, string> = {
  runId: 'run_id',
  traceId: 'trace_id',
  evidenceTraceId: 'evidence_trace_id',
  assetId: 'asset_id',
  assetRevision: 'asset_revision',
  experimentId: 'experiment_id',
  proposalId: 'proposal_id',
  promptText: 'prompt_text',
  score: 'score',
};

function clean(value: unknown): string | undefined {
  if (value === null || value === undefined) return undefined;
  const text = String(value).trim();
  return text.length > 0 ? text : undefined;
}

function readFirst(params: URLSearchParams, key: string): string | undefined {
  return clean(params.get(key));
}

export function viewForWorkbenchJourneyTarget(target: WorkbenchJourneyTarget): WorkbenchJourneyView {
  return VIEW_BY_TARGET[target];
}

export function buildWorkbenchJourneyHref(
  target: WorkbenchJourneyTarget,
  params: WorkbenchJourneyParams = {},
): string {
  const view = VIEW_BY_TARGET[target];
  const projectId = encodeURIComponent(clean(params.projectId) ?? 'default');
  const query = new URLSearchParams();
  for (const [paramKey, queryKey] of Object.entries(QUERY_KEYS)) {
    const value = clean(params[paramKey as keyof WorkbenchJourneyParams]);
    if (value !== undefined) query.set(queryKey, value);
  }
  const suffix = query.toString();
  return `/projects/${projectId}/${view}${suffix ? `?${suffix}` : ''}#${view}`;
}

export function readWorkbenchJourneyState(
  search: string | URLSearchParams,
  fallbackProjectId = 'default',
): WorkbenchJourneyState {
  const params = typeof search === 'string' ? new URLSearchParams(search) : search;
  return {
    projectId: readFirst(params, 'project_id') ?? clean(fallbackProjectId) ?? 'default',
    runId: readFirst(params, QUERY_KEYS.runId),
    traceId: readFirst(params, QUERY_KEYS.traceId),
    evidenceTraceId: readFirst(params, QUERY_KEYS.evidenceTraceId),
    assetId: readFirst(params, QUERY_KEYS.assetId),
    assetRevision: readFirst(params, QUERY_KEYS.assetRevision),
    experimentId: readFirst(params, QUERY_KEYS.experimentId),
    proposalId: readFirst(params, QUERY_KEYS.proposalId),
    promptText: readFirst(params, QUERY_KEYS.promptText),
    score: readFirst(params, QUERY_KEYS.score),
  };
}
