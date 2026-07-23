export class EvidenceRequiredError extends Error {
  constructor(context, offenders) {
    super(`Evidence guard: ${context} contains invalid refs: ${offenders.join(', ')}`);
    this.name = 'EvidenceRequiredError';
    this.context = context;
    this.offenders = offenders;
  }
}

export const PLACEHOLDER_PATTERNS = [
  /^workbench-status:/,
  /^ui:/,
  /:ui$/,
  /^ui-demo$/,
  /lease-ui-demo/,
  /policy:ui-demo/,
  /trace:ui-demo/,
  /eval:ui-demo/,
  /repro:ui-demo/,
  /context:ui-demo/,
  /artifact:ui-demo/,
  /checkpoint-ui-demo/,
  /snapshot:\/\/ui-demo/,
  /sha256-ui-demo/,
  /^preview-/,
  /no-runtime-binding-available/,
];

function normalizeRefs(refs) {
  if (refs == null) {
    return [];
  }
  const values = Array.isArray(refs) ? refs : [refs];
  return values
    .flatMap((value) => (Array.isArray(value) ? value : [value]))
    .map((value) => {
      if (typeof value === 'string') {
        return value.trim();
      }
      if (value && typeof value === 'object' && typeof value.ref === 'string') {
        return value.ref.trim();
      }
      return value;
    })
    .filter((value) => value !== undefined && value !== null && value !== '');
}

export function requireEvidence(refs, context = 'submission') {
  const cleanedRefs = normalizeRefs(refs);
  const offenders = cleanedRefs.filter(
    (ref) => typeof ref !== 'string' || PLACEHOLDER_PATTERNS.some((pattern) => pattern.test(ref)),
  );

  if (offenders.length > 0) {
    throw new EvidenceRequiredError(context, offenders.map(String));
  }

  return cleanedRefs;
}

function valuesForSegment(nodes, segment) {
  if (segment === '*') {
    return nodes.flatMap((node) => {
      if (!node || typeof node !== 'object') {
        return [];
      }
      return Object.values(node);
    });
  }

  const isArraySegment = segment.endsWith('[]');
  const key = isArraySegment ? segment.slice(0, -2) : segment;

  return nodes.flatMap((node) => {
    if (!node || typeof node !== 'object' || !(key in node)) {
      return [];
    }
    const value = node[key];
    if (isArraySegment) {
      return Array.isArray(value) ? value : [];
    }
    return [value];
  });
}

function valuesAtPath(payload, path) {
  return path
    .split('.')
    .filter(Boolean)
    .reduce((nodes, segment) => valuesForSegment(nodes, segment), [payload]);
}

export function assertNoPlaceholders(payload, fieldPaths, context = 'payload') {
  for (const path of fieldPaths) {
    requireEvidence(valuesAtPath(payload, path), `${context}:${path}`);
  }
}

export function buildReadinessSignals(snapshot, kinds = []) {
  const results = Array.isArray(snapshot?.results) ? snapshot.results : [];

  return Object.fromEntries(
    kinds.map((kind) => {
      const result = results.find((entry) => entry?.domain === kind);
      if (!result) {
        return [
          kind,
          {
            status: 'unknown',
            evidence_refs: [],
          },
        ];
      }

      return [
        kind,
        {
          status: result.status ?? 'unknown',
          summary: result.summary,
          evidence_refs: normalizeRefs(result.evidence_refs ?? result.evidence),
        },
      ];
    }),
  );
}
