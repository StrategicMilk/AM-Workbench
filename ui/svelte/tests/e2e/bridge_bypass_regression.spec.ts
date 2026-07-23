import { expect, test } from '@playwright/test';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../../..');

async function source(relativePath: string): Promise<string> {
  return fs.readFile(path.join(repoRoot, relativePath), 'utf8');
}

test.describe('native bridge bypass regression predicates', () => {
  test('model hub actions are exposed only through native-kernel-aware API wrappers', async () => {
    const api = await source('ui/svelte/src/lib/api.js');

    expect(api).toContain('export function searchModelHub(query)');
    expect(api).toContain('export function pullModelFromHub(spec)');
    expect(api).toContain("get(`${API}/models/hub/search");
    expect(api).toContain("post(`${API}/models/hub/pull`, spec)");
  });

  test('workbench playground posts through workbenchKernelRequest rather than raw fetch', async () => {
    const playground = await source('ui/svelte/src/views/WorkbenchPlayground.svelte');

    expect(playground).toContain("workbenchKernelRequest('/api/workbench/playground/experiments')");
    expect(playground).toContain("postPlaygroundJson('/api/workbench/playground/trace-to-eval')");
    expect(playground).toContain("postPlaygroundJson('/api/workbench/playground/run')");
    expect(playground).not.toContain('await fetch(url');
  });

  test('fetch bridge owns model hub and playground prefixes for Tauri-hosted browser calls', async () => {
    const routes = await source('ui/svelte/src/lib/native_kernel_routes.js');
    const bridge = await source('ui/svelte/src/lib/tauri_fetch_bridge.js');

    expect(routes).toContain("'/api/models'");
    expect(routes).toContain("'/api/workbench/playground'");
    expect(bridge).toContain("invoke('vetinari_kernel_request'");
    expect(bridge).toContain('return originalFetch(input, init)');
  });
});
