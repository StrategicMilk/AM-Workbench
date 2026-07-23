import { describe, expect, it } from 'vitest';

import { __extensionInstallGuardsForTest } from '../api.js';

const { assertMarketplaceExtensionInstall } = __extensionInstallGuardsForTest;

describe('assertMarketplaceExtensionInstall', () => {
  it('rejects local manifest entrypoints from the UI import path', () => {
    expect(() =>
      assertMarketplaceExtensionInstall({
        manifest: {
          name: 'local-extension',
          entrypoint: 'local:load',
          marketplace_ref: 'marketplace:local-extension',
        },
      }),
    ).toThrow('local extension manifest entrypoints are not installable from the UI');
  });

  it('rejects filesystem manifest entrypoints from the UI import path', () => {
    for (const entrypoint of ['file:///tmp/ext.js', '../ext/index.js', 'C:/extensions/ext.js', '\\\\share\\ext.js']) {
      expect(() =>
        assertMarketplaceExtensionInstall({
          manifest: {
            name: `bad-${entrypoint}`,
            entrypoint,
            marketplace_ref: 'marketplace:bad-extension',
          },
        }),
      ).toThrow('local extension manifest entrypoints are not installable from the UI');
    }
  });

  it('rejects raw manifests without marketplace provenance', () => {
    expect(() =>
      assertMarketplaceExtensionInstall({
        manifest: {
          name: 'unsigned-extension',
          entrypoint: 'https://extensions.example/unsigned.js',
        },
      }),
    ).toThrow('extension manifest installs require marketplace_ref provenance');
  });

  it('normalizes marketplace extension id installs without a raw manifest', () => {
    expect(
      assertMarketplaceExtensionInstall({
        extension_id: 'safe-doc-reader',
      }),
    ).toEqual({
      extension_id: 'safe-doc-reader',
      marketplace_ref: 'marketplace:safe-doc-reader',
      manifest: undefined,
    });
  });
});
