import { describe, expect, it } from 'vitest';

import {
  EvidenceRequiredError,
  assertNoPlaceholders,
  buildReadinessSignals,
  requireEvidence,
} from './evidenceGuard.js';

describe('requireEvidence', () => {
  it('rejects workbench status placeholders', () => {
    expect(() => requireEvidence(['workbench-status:setup'], 'test')).toThrow(EvidenceRequiredError);
  });

  it('rejects ui-prefixed refs', () => {
    expect(() => requireEvidence(['ui:workbench-status'], 'test')).toThrow(EvidenceRequiredError);
  });

  it('rejects ui demo trace refs', () => {
    expect(() => requireEvidence(['trace:ui-demo'], 'test')).toThrow(EvidenceRequiredError);
  });

  it('rejects preview refs', () => {
    expect(() => requireEvidence(['preview-tax'], 'test')).toThrow(EvidenceRequiredError);
  });

  it('returns known-good real refs', () => {
    expect(requireEvidence(['trace:real-run-abc123'], 'test')).toEqual(['trace:real-run-abc123']);
  });

  it('allows empty refs so callers own emptiness policy', () => {
    expect(requireEvidence([], 'test')).toEqual([]);
  });

  it('coerces a string ref before validation', () => {
    expect(() => requireEvidence('workbench-status:setup', 'test')).toThrow(EvidenceRequiredError);
  });
});

describe('buildReadinessSignals', () => {
  it('returns unknown entries when snapshot is absent', () => {
    expect(buildReadinessSignals(null, ['setup'])).toEqual({
      setup: {
        status: 'unknown',
        evidence_refs: [],
      },
    });
  });

  it('maps matching snapshot results without self-seeding evidence', () => {
    expect(
      buildReadinessSignals(
        {
          results: [
            {
              domain: 'setup',
              status: 'passing',
              summary: 'ok',
              evidence_refs: [{ ref: 'real:ref' }],
            },
          ],
        },
        ['setup'],
      ),
    ).toEqual({
      setup: {
        status: 'passing',
        summary: 'ok',
        evidence_refs: ['real:ref'],
      },
    });
  });

  it('carries legacy evidence refs into readiness signals', () => {
    expect(
      buildReadinessSignals(
        {
          results: [
            {
              domain: 'setup',
              status: 'passing',
              summary: 'ok',
              evidence: [{ ref: 'trace:real-run-abc123' }],
            },
          ],
        },
        ['setup'],
      ),
    ).toEqual({
      setup: {
        status: 'passing',
        summary: 'ok',
        evidence_refs: ['trace:real-run-abc123'],
      },
    });
  });

  it('returns unknown for kinds missing from the snapshot', () => {
    expect(buildReadinessSignals({ results: [] }, ['setup'])).toEqual({
      setup: {
        status: 'unknown',
        evidence_refs: [],
      },
    });
  });
});

describe('assertNoPlaceholders', () => {
  it('rejects nested placeholder refs', () => {
    const payload = {
      readiness_signals: {
        setup: {
          evidence_refs: [{ ref: 'workbench-status:setup' }],
        },
      },
    };

    expect(() =>
      assertNoPlaceholders(payload, ['readiness_signals.*.evidence_refs[].ref'], 'test'),
    ).toThrow(EvidenceRequiredError);
  });

  it('accepts nested real refs', () => {
    const payload = {
      evidence_links: [{ ref: 'trace:real-run-abc123' }],
      readiness_signals: {
        setup: {
          evidence_refs: [{ ref: 'artifact:real-run-abc123' }],
        },
      },
    };

    expect(() =>
      assertNoPlaceholders(
        payload,
        ['evidence_links[].ref', 'readiness_signals.*.evidence_refs[].ref'],
        'test',
      ),
    ).not.toThrow();
  });
});
