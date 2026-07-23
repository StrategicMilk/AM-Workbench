import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import {
  CAPABILITY_PRODUCT_CONTRACT_ID,
  CAPABILITY_PRODUCT_SOURCE_IDS,
  ContractViolationError,
  validateCapabilityProductClosure,
} from '../src/lib/api_contract.js';
import { installExtension, listExtensions, submitIntakeWizard, uploadRagDocument } from '../src/lib/api.js';
import { createFailClosedAsyncStore } from '../src/lib/async_store.svelte.js';
import { nativeKernelPathFromUrl, nativeProjectStreamPath } from '../src/lib/native_kernel_routes.js';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');

function validClosure() {
  return {
    contract_id: CAPABILITY_PRODUCT_CONTRACT_ID,
    fail_closed: true,
    source_ids: [...CAPABILITY_PRODUCT_SOURCE_IDS],
    surfaces: CAPABILITY_PRODUCT_SOURCE_IDS.map((sourceId) => ({
      source_id: sourceId,
      path: `surface/${sourceId}`,
      status: 'resolved',
      evidence: ['tests/test_rcg_0021_p01.py'],
    })),
    verification: {
      command: '.venv312/Scripts/python.exe -m pytest tests/test_rcg_0021_p01.py',
      passed: true,
    },
  };
}

test('capability product closure rejects missing source ids', () => {
  const closure = validClosure();
  closure.source_ids = closure.source_ids.slice(1);
  assert.throws(() => validateCapabilityProductClosure(closure), ContractViolationError);
});

test('capability product closure accepts complete terminal evidence', () => {
  const normalized = validateCapabilityProductClosure(validClosure());
  assert.equal(normalized.contractId, CAPABILITY_PRODUCT_CONTRACT_ID);
  assert.equal(normalized.surfaces.length, CAPABILITY_PRODUCT_SOURCE_IDS.length);
});

test('async store blocks when loader or validation is unavailable', async () => {
  const missingLoader = createFailClosedAsyncStore({ label: 'contract fixture' });
  await assert.rejects(() => missingLoader.refresh(), ContractViolationError);
  assert.equal(missingLoader.status, 'blocked');

  const invalidPayload = createFailClosedAsyncStore({
    label: 'contract fixture',
    loader: async () => ({ ok: false }),
    validate(value) {
      if (!value.ok) throw new ContractViolationError('invalid fixture');
      return value;
    },
  });
  await assert.rejects(() => invalidPayload.refresh(), ContractViolationError);
  assert.equal(invalidPayload.status, 'blocked');
});

test('workbench extension helpers use native kernel endpoints', async () => {
  const calls = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return new Response(JSON.stringify({ extension: { extension_id: 'local-extension' }, extensions: [] }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  };

  try {
    await listExtensions();
    await installExtension({ manifest: { name: 'local-extension', version: '0.1.0' } });
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.equal(calls[0].url, '/api/workbench/extensions');
  assert.equal(calls[1].url, '/api/workbench/extensions/import');
  assert.equal(JSON.parse(calls[1].options.body).extension_id, 'local-extension');
});

test('rag upload helper uses native workbench route', async () => {
  const calls = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return new Response(JSON.stringify({ doc_id: 'doc-1', ingested_chunks: 1, source: 'upload' }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  };

  try {
    await uploadRagDocument(new Blob(['hello'], { type: 'text/plain' }));
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.equal(calls[0].url, '/api/workbench/rag/documents');
  assert.equal(calls[0].options.method, 'POST');
  assert.equal(calls[0].options.body instanceof FormData, true);
});

test('intake wizard helper uses native kernel intake route', async () => {
  const calls = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return new Response(JSON.stringify({ request_frame: { goal: 'ship', raw_prompt: 'ship' }, worker_mode_cluster: [] }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  };

  try {
    await submitIntakeWizard({ raw_prompt: 'ship', persona_name: null });
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.equal(calls[0].url, '/api/intake/wizard');
  assert.equal(calls[0].options.method, 'POST');
});

test('model hub routes are owned by the native kernel bridge', () => {
  assert.equal(nativeKernelPathFromUrl('/api/models/hub/search?q=llama'), '/api/models/hub/search?q=llama');
  assert.equal(nativeKernelPathFromUrl('/api/models/hub/pull'), '/api/models/hub/pull');
});

test('project event stream uses native project workbench route', () => {
  const streamPath = nativeProjectStreamPath('demo project');
  assert.equal(streamPath, '/api/v1/projects/demo%20project/workbench/stream');
  assert.equal(nativeKernelPathFromUrl(streamPath), streamPath);

  const sseSource = readFileSync(resolve(ROOT, 'src/lib/stores/sse.svelte.js'), 'utf8');
  assert.match(sseSource, /nativeProjectStreamPath/);
  assert.doesNotMatch(sseSource, /\/api\/project\/.*\/stream/);
});

test('project route view changes replace stale path segments', () => {
  const appSource = readFileSync(resolve(ROOT, 'src/App.svelte'), 'utf8');
  assert.match(appSource, /function projectPathFor\(view\)/);
  assert.match(appSource, /\/projects\/\$\{encodeURIComponent\(projectId\)\}\/\$\{encodeURIComponent\(view\)\}/);
  assert.match(appSource, /window\.history\.replaceState\(null, '', nextUrl\)/);
  assert.doesNotMatch(appSource, /\$activeView = next;/);
});

test('ui test contracts do not name retired litestar backend', () => {
  const sources = [
    'tests/e2e/help_a11y.spec.ts',
    'tests/e2e/attention-track.spec.js',
    'src/lib/api.js',
  ];
  for (const sourcePath of sources) {
    assert.doesNotMatch(readFileSync(resolve(ROOT, sourcePath), 'utf8'), /Litestar|litestar_/);
  }
});

test('plan builder status refresh fails closed on backend errors', () => {
  const sources = [
    'src/views/PlanBuilderView.svelte',
    'src/components/views/planbuilder/PlanBuilderWorkspace.svelte',
  ];
  for (const sourcePath of sources) {
    const source = readFileSync(resolve(ROOT, sourcePath), 'utf8');
    assert.doesNotMatch(source, /stale display is preferable/);
    assert.match(source, /planStatus\s*=\s*null;/);
    assert.match(source, /planStatusError\s*=/);
  }
});
