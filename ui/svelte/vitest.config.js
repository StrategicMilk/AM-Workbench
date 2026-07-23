import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { svelte } from '@sveltejs/vite-plugin-svelte';
import { defineConfig } from 'vite';

const packageRoot = path.dirname(fileURLToPath(import.meta.url));
const srcRoot = path.resolve(packageRoot, 'src');

export default defineConfig({
  plugins: [
    svelte({
      hot: false,
      compilerOptions: {
        runes: true,
      },
      extensions: ['.svelte', '.svelte.js', '.svelte.ts'],
    }),
  ],
  resolve: {
    alias: {
      $lib: path.resolve(srcRoot, 'lib'),
      $components: path.resolve(srcRoot, 'components'),
      $views: path.resolve(srcRoot, 'views'),
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/tests/setup.js'],
    include: ['src/**/*.{test,spec}.{js,ts}', 'tests/unit/**/*.{test,spec}.{js,ts}'],
    clearMocks: true,
    restoreMocks: true,
  },
});
