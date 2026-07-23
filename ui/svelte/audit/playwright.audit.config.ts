/**
 * Isolated Playwright configuration for the Vetinari UI audit lanes.
 *
 * IMPORTANT: This file is for PROGRAMMATIC use by the Python audit scripts
 * (run_axe_lane.py, run_lighthouse_lane.py) — NOT for `npx playwright test`.
 * The main Svelte test suite uses `ui/svelte/playwright.config.js` instead.
 *
 * The audit scripts launch Chromium via the Playwright Python package API;
 * this config is referenced for channel and headless settings when the scripts
 * invoke Playwright sub-processes.
 */

import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  // Placeholder testDir — audit scripts do not use Playwright's test runner
  // directly; they use the Playwright API programmatically via run_axe_lane.py.
  testDir: "./tests",

  use: {
    // Always run headless in audit mode — no visible browser window.
    headless: true,
    // Use the stable Chromium channel for reproducible audit results.
    channel: "chromium",
  },

  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
      },
    },
  ],
});
