import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { defineConfig, devices } from '@playwright/test';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const uiRoot = path.resolve(__dirname, '../..');

export default defineConfig({
  testDir: '.',
  testMatch: ['pack_a11y.test.ts'],
  timeout: 30_000,
  expect: {
    timeout: 5_000,
  },
  fullyParallel: true,
  reporter: 'list',
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
    command: 'npx vite dev --host 127.0.0.1 --port 5174 --strictPort',
    cwd: uiRoot,
    url: 'http://127.0.0.1:5174',
    timeout: 120_000,
    reuseExistingServer: !process.env.CI,
  },
});
