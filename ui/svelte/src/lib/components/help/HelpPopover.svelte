<script>
  /**
   * Contextual help popover with focus trap and WCAG 2.1 AA keyboard support.
   *
   * When severity="critical" the full title+body is rendered inline as an
   * <aside role="note"> — always visible, no trigger required.
   *
   * For all other severities a trigger button toggles a dialog popover.
   * The popover traps focus (Tab/Shift+Tab cycle inside; Escape closes and
   * returns focus to the trigger).
   *
   * density prop is accepted directly for independent testability before the
   * uiPreferences store is wired; the store value overrides when available.
   */
  import { uiPreferences } from '$lib/stores/uiPreferences.svelte.js';

  const { title, body, severity = 'info', id = undefined, density: densityProp = undefined } = $props();

  function stableIdPart(value, fallback) {
    return (
      String(value ?? fallback)
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, '-')
        .replace(/^-+|-+$/g, '') || fallback
    );
  }

  // Prefer store value; fall back to prop; default to 'standard'.
  let density = $derived(uiPreferences.helpDensity ?? densityProp ?? 'standard');

  // Stable ids for aria wiring. $derived keeps them reactive if props change.
  let _fallbackId = $derived(`help-popover-${stableIdPart(title, 'untitled')}-${stableIdPart(body, 'body')}`);
  let popoverId = $derived(id ?? _fallbackId);
  let titleId = $derived(`${popoverId}-title`);

  // Verbose mode: popover body open by default.
  let open = $state(false);
  $effect(() => {
    if (density === 'verbose') {
      open = true;
    }
  });

  // Under compact density non-critical popovers are suppressed entirely.
  let suppressed = $derived(
    severity !== 'critical' && density === 'compact'
  );

  let triggerEl = $state(null);
  let popoverEl = $state(null);

  function toggle() { open = !open; }

  function close() {
    open = false;
    triggerEl?.focus();
  }

  function handleTriggerKeydown(event) {
    if (event.key === 'Escape' && open) {
      event.preventDefault();
      close();
    }
  }

  /**
   * Focus trap: keep Tab and Shift+Tab cycling inside the open popover;
   * Escape closes and returns focus to the trigger.
   */
  function handlePopoverKeydown(event) {
    if (event.key === 'Escape') {
      event.preventDefault();
      close();
      return;
    }
    if (event.key === 'Tab' && popoverEl) {
      const focusable = Array.from(
        popoverEl.querySelectorAll(
          'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
        )
      ).filter((el) => !el.hasAttribute('disabled'));
      if (focusable.length === 0) {
        event.preventDefault();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey) {
        if (document.activeElement === first) {
          event.preventDefault();
          last.focus();
        }
      } else {
        if (document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
    }
  }
</script>

{#if severity === 'critical'}
  <!-- Critical: always visible inline regardless of density. -->
  <aside
    role="note"
    class="help-popover help-popover--critical"
    aria-label={title}
  >
    <strong class="help-popover-title">{title}</strong>
    <p class="help-popover-body">{body}</p>
  </aside>
{:else if !suppressed}
  <span class="help-popover-wrap">
    <button
      type="button"
      class="help-popover-trigger"
      bind:this={triggerEl}
      aria-expanded={open}
      aria-controls={popoverId}
      aria-haspopup="dialog"
      onclick={toggle}
      onkeydown={handleTriggerKeydown}
    >
      <span aria-hidden="true" class="help-icon">?</span>
      <span class="help-popover-trigger-label">{title}</span>
    </button>

    {#if open}
      <!-- svelte-ignore a11y_no_static_element_interactions -->
      <div
        id={popoverId}
        role="dialog"
        aria-modal="false"
        aria-labelledby={titleId}
        class="help-popover-dialog"
        tabindex="-1"
        bind:this={popoverEl}
        onkeydown={handlePopoverKeydown}
      >
        <div class="help-popover-header">
          <strong id={titleId} class="help-popover-title">{title}</strong>
          <button
            type="button"
            class="help-popover-close"
            onclick={close}
            aria-label="Close help popover"
          >
            <span aria-hidden="true">×</span>
          </button>
        </div>
        <p class="help-popover-body">{body}</p>
      </div>
    {/if}
  </span>
{/if}

<style>
  /* Critical variant — always visible inline */
  .help-popover--critical {
    display: block;
    padding: 10px 14px;
    border-radius: var(--radius-sm, 4px);
    background: #5f1f28;
    color: #fff5f5;
    border: 1px solid rgba(255, 154, 154, 0.55);
    font-size: 0.875rem;
  }

  .help-popover--critical .help-popover-title {
    display: block;
    font-weight: 600;
    margin-bottom: 4px;
    color: #fff5f5;
  }

  .help-popover--critical .help-popover-body {
    margin: 0;
    line-height: 1.45;
    color: #ffe4e6;
  }

  /* Non-critical trigger */
  .help-popover-wrap {
    position: relative;
    display: inline-flex;
    align-items: center;
  }

  .help-popover-trigger {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 4px 8px;
    border-radius: var(--radius-sm, 4px);
    border: 1px solid var(--border-default, #ccc);
    background: var(--surface-hover, #f5f5f5);
    color: var(--text-muted, #888);
    font-size: 0.8125rem;
    font-weight: 500;
    cursor: pointer;
    font-family: inherit;
    outline: 2px solid transparent;
    outline-offset: 2px;
  }

  .help-popover-trigger:focus-visible {
    outline: 2px solid var(--primary, #4a90e2);
    outline-offset: 2px;
  }

  .help-icon { font-size: 0.75rem; font-weight: 700; }

  .help-popover-trigger-label { font-size: 0.8125rem; }

  /* Dialog popover */
  .help-popover-dialog {
    position: absolute;
    top: calc(100% + 6px);
    left: 0;
    z-index: 200;
    min-width: 240px;
    max-width: 360px;
    padding: 12px 14px;
    border-radius: var(--radius-md, 6px);
    background: var(--surface-elevated, #1e1e1e);
    color: var(--text-primary, #f0f0f0);
    border: 1px solid var(--border-default, #444);
    box-shadow: 0 4px 16px rgba(0, 0, 0, 0.35);
  }

  /* Respect prefers-reduced-motion — no transitions on open/close */
  @media (prefers-reduced-motion: reduce) {
    .help-popover-dialog {
      transition: none;
      animation: none;
    }
  }

  .help-popover-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 8px;
    margin-bottom: 8px;
  }

  .help-popover-title {
    font-size: 0.9375rem;
    font-weight: 600;
    color: var(--text-primary, #f0f0f0);
    line-height: 1.3;
  }

  .help-popover-close {
    flex-shrink: 0;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 22px;
    height: 22px;
    border-radius: var(--radius-sm, 4px);
    border: none;
    background: transparent;
    color: var(--text-muted, #888);
    font-size: 1.125rem;
    cursor: pointer;
    font-family: inherit;
    outline: 2px solid transparent;
    outline-offset: 2px;
  }

  .help-popover-close:focus-visible {
    outline: 2px solid var(--primary, #4a90e2);
    outline-offset: 2px;
  }

  .help-popover-close:hover { color: var(--text-primary, #f0f0f0); }

  .help-popover-body {
    margin: 0;
    font-size: 0.875rem;
    line-height: 1.5;
    color: var(--text-secondary, #ccc);
  }
</style>
