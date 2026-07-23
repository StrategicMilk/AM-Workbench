import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { clampPercent, clampUnit, unitPercent } from '../uiGuards.js';

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), '../../../../..');

function source(path) {
  return readFileSync(resolve(repoRoot, path), 'utf8');
}

describe('uiGuards numeric normalization', () => {
  it('bounds percentage values before Svelte progress controls receive them', () => {
    expect(clampPercent(-25, 0)).toBe(0);
    expect(clampPercent(147, 0)).toBe(100);
    expect(clampPercent('not-a-number', 0)).toBe(0);
    expect(clampPercent(42.5, 0)).toBe(42.5);
  });

  it('bounds unit scores before Svelte meters or confidence labels receive them', () => {
    expect(clampUnit(-0.2, 0)).toBe(0);
    expect(clampUnit(1.7, 0)).toBe(1);
    expect(clampUnit(Number.NaN, 0)).toBe(0);
    expect(clampUnit(0.42, 0)).toBe(0.42);
  });

  it('formats bounded confidence scores as display percentages', () => {
    expect(unitPercent(-0.2, 0)).toBe(0);
    expect(unitPercent(1.7, 0)).toBe(100);
    expect(unitPercent('not-a-number', 0)).toBe(0);
    expect(unitPercent(0.424, 0)).toBe(42);
  });
});

describe('RCG-0039-P02 closure source predicates', () => {
  it('keeps memory scope and promotion surfaces wired to live API data with degraded fallbacks', () => {
    const memoryScopes = source('ui/svelte/src/lib/components/workbench/memory_scopes/MemoryScopesPanel.svelte');
    const promotionEngine = source('ui/svelte/src/lib/components/workbench/promotions/PromotionEngine.svelte');

    expect(memoryScopes).toContain('api.getMemoryScopes(projectId)');
    expect(memoryScopes).toContain("scopeState = 'api'");
    expect(memoryScopes).toContain("scopeState = 'blocked'");
    expect(memoryScopes).toContain('onScopeChange?.');

    expect(promotionEngine).toContain('api.getPromotionRecipes(projectId)');
    expect(promotionEngine).toContain("recipeState = rows.length ? 'api' : 'blocked'");
    expect(promotionEngine).toContain('onPromoteRecipe?.');
  });

  it('bounds heavy and malformed RAG/memory rendering paths before display or replay', () => {
    const memoryEntries = source('ui/svelte/src/components/views/memory/MemoryEntriesList.svelte');
    const ragQuery = source('ui/svelte/src/components/workbench/RagQueryPanel.svelte');
    const ragContext = source('ui/svelte/src/components/workbench/RagContextAssembly.svelte');

    expect(memoryEntries).toContain('const MAX_RENDERED_ENTRIES = 200');
    expect(memoryEntries).toContain('filteredEntries.slice(0, MAX_RENDERED_ENTRIES)');
    expect(ragQuery).toContain('api.getRagQueryDefaults(projectId)');
    expect(ragQuery).toContain('revision_id: selectedRevisionId');
    expect(ragQuery).toContain('bind:value={selectedRevisionId}');
    expect(ragContext).toContain("'No grounding verdict'");
    expect(ragContext).toContain("verdictState === 'empty'");
  });

  it('normalizes route and policy contracts at the workbench boundary', () => {
    const artifactDrawer = source('ui/svelte/src/components/workbench/ArtifactDrawer.svelte');
    const policyBadge = source('ui/svelte/src/components/workbench/PolicyBadge.svelte');
    const tauriBridge = source('ui/svelte/src/lib/tauri_fetch_bridge.js');

    expect(artifactDrawer).toContain('rowsFromResponse(evalRows,');
    expect(artifactDrawer).toContain('Array.isArray(value?.items)');
    expect(policyBadge).toContain('normalizePolicyDecision(metric.value)');
    expect(policyBadge).toContain("['allow', 'allowed', 'pass', 'passed', 'success', 'true']");
    expect(policyBadge).toContain("['deny', 'denied', 'block', 'blocked', 'fail', 'failed', 'false']");
    expect(tauriBridge).toContain("error: 'native_kernel_route_unavailable'");
    expect(tauriBridge).toContain("nativeKernelRejected(path, payload)");
  });
});
