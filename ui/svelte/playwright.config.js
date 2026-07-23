// Playwright config for the Vetinari UI smoke suite.
//
// The tests run against `vite dev` on a fixed port (5174) because the
// production shell is owned by the Tauri/Rust desktop host. Pair this with
// `npm run build` for production-bundle smoke coverage. Backend API calls are
// mocked per-test via `page.route()` so no backend server is needed.
//
// First-time setup on a developer machine:
//   npm install
//   npm run test:e2e:install   # downloads the Chromium binary
//
// Running the suite:
//   npm run test:e2e

import Module from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { defineConfig, devices } from '@playwright/test';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
process.env.NODE_PATH = [path.join(__dirname, 'node_modules'), process.env.NODE_PATH].filter(Boolean).join(path.delimiter);
Module._initPaths();

export default defineConfig({
  testDir: '.',
  testMatch: ['tests/e2e/**/*.{spec,test}.{js,ts}', 'tests/a11y/**/*.{spec,test}.{js,ts}'],
  testIgnore: ['.claude/**', '.ai-codex/**', '.codex/**'],
  timeout: 30_000,
  expect: {
    timeout: 5_000,
  },
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? 'github' : 'list',

  use: {
    baseURL: 'http://127.0.0.1:5174',
    trace: 'on-first-retry',
  },

  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],

  webServer: {
    // Smoke against `vite dev`, not `vite preview` — the production build
    // emits chunks to ../static/svelte/ (no standalone index.html; the
    // Tauri/Rust host owns the production shell). The dev server uses ui/svelte/index.html
    // directly and bundles src/main.js on demand, which is the only
    // self-contained way to serve the SPA without the backend running.
    //
    // Port 5174 (dev default + 1) avoids colliding with a developer's
    // running `npm run dev` on 5173.
    //
    // /api/* calls are mocked via page.route() in each spec, so the dev
    // server's /api proxy is never exercised.
    command: 'npx vite dev --host 127.0.0.1 --port 5174 --strictPort',
    url: 'http://127.0.0.1:5174',
    timeout: 120_000,
    reuseExistingServer: !process.env.CI,
  },
});
