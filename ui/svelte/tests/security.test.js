import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

import {
  provenanceDecision,
  redactSupplyChainPayload,
  requireTrustedProvenance,
} from '../src/lib/security/index.js';

describe('supply-chain UI security boundary', () => {
  it('fails closed when provenance refs are missing or placeholder-only', () => {
    expect(provenanceDecision({}, 'missing').state).toBe('blocked');
    expect(provenanceDecision({ evidence_refs: ['ui-demo'] }, 'placeholder').state).toBe('blocked');
    expect(() => requireTrustedProvenance({ evidence_refs: ['ui-demo'] }, 'placeholder')).toThrow();
  });

  it('accepts trusted package and policy refs for UI supply-chain actions', () => {
    const decision = provenanceDecision({
      evidence_refs: ['npm:chart.js@4.5.1', 'policy:capability-pack-default'],
      status: 'verified',
    }, 'trusted-action');

    expect(decision.trusted).toBe(true);
    expect(() => requireTrustedProvenance({
      evidence_refs: ['npm:chart.js@4.5.1'],
      status: 'verified',
    }, 'trusted-action')).not.toThrow();
  });

  it('redacts local paths and secret values recursively before UI state stores them', () => {
    const payload = redactSupplyChainPayload({
      bundle_path: 'C:/Users/example/AppData/Local/support.zip',
      nested: { api_token: 'preview-secret', note: 'Bearer abc.def.ghi' },
    });

    expect(payload.bundle_path).toBe('<redacted-local-path>');
    expect(payload.nested.api_token).toBe('<redacted-secret>');
    expect(payload.nested.note).toBe('Bearer <redacted>');
  });

  it('keeps OutputView on bundled highlight.js instead of runtime CDN injection', () => {
    const source = readFileSync(new URL('../src/views/OutputView.svelte', import.meta.url), 'utf8');

    expect(source).toContain("from 'highlight.js/lib/core'");
    expect(source).not.toMatch(/cdnjs|unpkg|jsdelivr|<script/i);
  });
});
