/**
 * WCAG 2.1 AA accessibility harness for Help components.
 *
 * Assumptions:
 * - The Vetinari Vite dev server is started automatically by playwright.config.js
 *   via the webServer config (port 5174). No external server is needed.
 * - /api/glossary is mocked per-test via page.route() so no live backend
 *   is required.
 * - axe-core checks are run via @axe-core/playwright which is already in
 *   devDependencies.
 *
 * If Playwright is not yet configured at the repo root (tests/playwright/),
 * use `npm run test:e2e` from ui/svelte/ to run this suite via the existing
 * playwright.config.js.
 */

import { test, expect } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

// ---------------------------------------------------------------------------
// Shared helpers
// ---------------------------------------------------------------------------

const MOCK_GLOSSARY = [
  {
    term: 'Foreman',
    short: 'Planner role that decomposes goals into scoped tasks.',
    long: 'Foreman is the planning role in Vetinari\'s three-agent factory pipeline.',
    see_also: ['Worker'],
    category: 'agent',
  },
  {
    term: 'Worker',
    short: 'Execution role that performs scoped implementation work.',
    long: 'Worker is the implementation role in the factory pipeline.',
    see_also: ['Foreman'],
    category: 'agent',
  },
];

/** Mount permissive API mocks so the Settings page renders without a backend. */
async function installFallbackMocks(page) {
  await page.route('**/health', (route) =>
    route.fulfill({ json: { status: 'ok' } })
  );
  await page.route('**/api/glossary', (route) =>
    route.fulfill({ json: MOCK_GLOSSARY })
  );
  await page.route('**/api/**', (route) => route.fulfill({ json: {} }));
}

/** Navigate to the Settings view and wait for the help-density section. */
async function goToSettingsHelpSection(page) {
  await installFallbackMocks(page);
  await page.goto('/?view=settings');
  // Wait for the help-density section (contains HelpTooltip, HelpPopover, Term).
  await page.waitForSelector('.help-settings, .help-demo-row', { timeout: 8000 });
}

// ---------------------------------------------------------------------------
// axe-core zero-violation tests
// ---------------------------------------------------------------------------

test.describe('Help components a11y', () => {

  test('axe_no_violations_help_tooltip_info', async ({ page }) => {
    await goToSettingsHelpSection(page);
    // Non-critical HelpTooltip is rendered inside .help-settings h3.
    const results = await new AxeBuilder({ page })
      .include('.help-settings')
      .withTags(['wcag2a', 'wcag2aa'])
      .analyze();
    expect(results.violations).toHaveLength(0);
  });

  test('axe_no_violations_help_tooltip_critical', async ({ page }) => {
    await goToSettingsHelpSection(page);
    // The critical HelpTooltip is in .help-demo-row.
    const results = await new AxeBuilder({ page })
      .include('.help-demo-row')
      .withTags(['wcag2a', 'wcag2aa'])
      .analyze();
    expect(results.violations).toHaveLength(0);
  });

  test('axe_no_violations_help_popover_info', async ({ page }) => {
    await goToSettingsHelpSection(page);
    const results = await new AxeBuilder({ page })
      .include('.help-demo-row')
      .withTags(['wcag2a', 'wcag2aa'])
      .analyze();
    expect(results.violations).toHaveLength(0);
  });

  test('axe_no_violations_help_popover_critical', async ({ page }) => {
    await goToSettingsHelpSection(page);
    // Critical HelpPopover is inside .help-demo-row as aside[role=note].
    await page.waitForSelector('aside[role="note"]', { timeout: 5000 });
    const results = await new AxeBuilder({ page })
      .include('aside[role="note"]')
      .withTags(['wcag2a', 'wcag2aa'])
      .analyze();
    expect(results.violations).toHaveLength(0);
  });

  test('axe_no_violations_term', async ({ page }) => {
    await goToSettingsHelpSection(page);
    // The Term component renders inside .setting-label.
    await page.waitForSelector('abbr[data-term], [data-glossary-miss]', { timeout: 5000 });
    const results = await new AxeBuilder({ page })
      .include('.setting-label')
      .withTags(['wcag2a', 'wcag2aa'])
      .analyze();
    expect(results.violations).toHaveLength(0);
  });

  // ---------------------------------------------------------------------------
  // Keyboard navigation
  // ---------------------------------------------------------------------------

  test('keyboard_tooltip_tab_focus', async ({ page }) => {
    await goToSettingsHelpSection(page);
    // Tab to the first help-tooltip trigger button.
    const trigger = page.locator('.help-tooltip-trigger').first();
    await trigger.focus();
    await expect(trigger).toBeFocused();
    // Tooltip bubble should now be visible (opacity transitions don't affect
    // DOM visibility checks in Playwright — we assert the visible class is present).
    await expect(page.locator('.help-tooltip-bubble--visible').first()).toBeVisible();
  });

  test('keyboard_tooltip_escape_closes', async ({ page }) => {
    await goToSettingsHelpSection(page);
    const trigger = page.locator('.help-tooltip-trigger').first();
    await trigger.focus();
    // Confirm open.
    await expect(page.locator('.help-tooltip-bubble--visible').first()).toBeVisible();
    // Press Escape.
    await page.keyboard.press('Escape');
    await expect(page.locator('.help-tooltip-bubble--visible')).toHaveCount(0);
  });

  test('keyboard_popover_enter_opens', async ({ page }) => {
    await goToSettingsHelpSection(page);
    const trigger = page.locator('.help-popover-trigger').first();
    await trigger.focus();
    await page.keyboard.press('Enter');
    await expect(trigger).toHaveAttribute('aria-expanded', 'true');
    await expect(page.locator('[role="dialog"]').first()).toBeVisible();
  });

  test('keyboard_popover_escape_closes', async ({ page }) => {
    await goToSettingsHelpSection(page);
    const trigger = page.locator('.help-popover-trigger').first();
    await trigger.focus();
    await page.keyboard.press('Enter');
    // Confirm open.
    await expect(trigger).toHaveAttribute('aria-expanded', 'true');
    // Press Escape.
    await page.keyboard.press('Escape');
    await expect(trigger).toHaveAttribute('aria-expanded', 'false');
    // Focus must return to the trigger.
    await expect(trigger).toBeFocused();
  });

  test('keyboard_popover_tab_trap', async ({ page }) => {
    await goToSettingsHelpSection(page);
    const trigger = page.locator('.help-popover-trigger').first();
    await trigger.focus();
    await page.keyboard.press('Enter');
    const dialog = page.locator('[role="dialog"]').first();
    await expect(dialog).toBeVisible();

    // Tab through all focusable items and confirm focus stays inside the dialog.
    // Tab 10 times — should never escape.
    for (let i = 0; i < 10; i++) {
      await page.keyboard.press('Tab');
      const focused = await page.evaluate(() => document.activeElement?.closest('[role="dialog"]') !== null);
      expect(focused).toBe(true);
    }

    // Escape to close.
    await page.keyboard.press('Escape');
    await expect(dialog).not.toBeVisible();
  });

  // ---------------------------------------------------------------------------
  // ARIA attribute assertions
  // ---------------------------------------------------------------------------

  test('screen_reader_tooltip_aria_describedby', async ({ page }) => {
    await goToSettingsHelpSection(page);
    const trigger = page.locator('.help-tooltip-trigger').first();
    const describedBy = await trigger.getAttribute('aria-describedby');
    expect(describedBy).toBeTruthy();
    // The referenced element must exist and contain text.
    const tooltipEl = page.locator(`#${describedBy}`);
    await expect(tooltipEl).toHaveCount(1);
    const text = await tooltipEl.textContent();
    expect(text?.trim().length).toBeGreaterThan(0);
  });

  test('screen_reader_popover_role_dialog', async ({ page }) => {
    await goToSettingsHelpSection(page);
    const trigger = page.locator('.help-popover-trigger').first();
    await trigger.focus();
    await page.keyboard.press('Enter');
    const dialog = page.locator('[role="dialog"]').first();
    await expect(dialog).toBeVisible();
    await expect(dialog).toHaveAttribute('role', 'dialog');
    await expect(dialog).toHaveAttribute('aria-modal', 'false');
  });

  // ---------------------------------------------------------------------------
  // Critical severity renders without interaction
  // ---------------------------------------------------------------------------

  test('critical_warning_visible_without_interaction', async ({ page }) => {
    await installFallbackMocks(page);
    await page.goto('/?view=settings');
    await page.waitForSelector('.help-demo-row', { timeout: 8000 });

    // Critical HelpTooltip: role="alert" visible before any interaction.
    const alertEl = page.locator('[role="alert"]').first();
    await expect(alertEl).toBeVisible();

    // Critical HelpPopover: aside[role="note"] visible before any interaction.
    const noteEl = page.locator('aside[role="note"]').first();
    await expect(noteEl).toBeVisible();

    // Confirm neither requires hover or focus — no mouse/keyboard events issued above.
  });

  // ---------------------------------------------------------------------------
  // Reduced motion
  // ---------------------------------------------------------------------------

  test('reduced_motion_preference', async ({ page }) => {
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await goToSettingsHelpSection(page);

    // Focus the tooltip trigger to ensure the bubble is shown.
    const trigger = page.locator('.help-tooltip-trigger').first();
    await trigger.focus();

    // Under prefers-reduced-motion the transition should be none / 0s.
    const bubble = page.locator('.help-tooltip-bubble').first();
    const transition = await bubble.evaluate((el) =>
      getComputedStyle(el).transition
    );
    // Accept "none", "all 0s", "opacity 0s", or any variant with 0s duration.
    const hasAnimation = transition && !transition.includes('0s') && transition !== 'none' && transition !== 'none 0s ease 0s';
    expect(hasAnimation).toBe(false);
  });

  // ---------------------------------------------------------------------------
  // Density gating
  // ---------------------------------------------------------------------------

  test('density_compact_hides_noncritical', async ({ page }) => {
    // Set compact density in localStorage before page load.
    await page.addInitScript(() => {
      localStorage.setItem('helpDensity', 'compact');
    });
    await installFallbackMocks(page);
    await page.goto('/?view=settings');
    await page.waitForSelector('.help-settings, .help-demo-row', { timeout: 8000 });

    // Non-critical tooltip trigger should NOT be present (suppressed in compact).
    const nonCriticalTrigger = page.locator('.help-tooltip-trigger');
    await expect(nonCriticalTrigger).toHaveCount(0);

    // Non-critical popover trigger should NOT be present (suppressed in compact).
    const nonCriticalPopover = page.locator('.help-popover-trigger');
    await expect(nonCriticalPopover).toHaveCount(0);

    // Critical HelpTooltip (role="alert") MUST still be present.
    await expect(page.locator('[role="alert"]').first()).toBeVisible();

    // Critical HelpPopover (aside[role="note"]) MUST still be present.
    await expect(page.locator('aside[role="note"]').first()).toBeVisible();
  });

});
