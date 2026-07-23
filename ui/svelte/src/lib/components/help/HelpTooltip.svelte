<script>
  /**
   * Inline or hover tooltip for contextual help.
   *
   * When severity="critical" the text is always visible as an inline alert.
   * For all other severities a trigger button reveals the tooltip on focus or
   * hover and dismisses it on Escape, following WCAG 2.1 SC 1.4.13.
   */
  import { uiPreferences } from '$lib/stores/uiPreferences.svelte.js';

  const { text, severity = 'info', id = undefined } = $props();

  function stableIdPart(value) {
    return (
      String(value ?? 'help')
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, '-')
        .replace(/^-+|-+$/g, '') || 'help'
    );
  }

  // Stable id for aria-describedby wiring when caller does not supply one.
  // $derived ensures the value reacts if the caller changes the id prop.
  let _fallbackId = $derived(`help-tooltip-${stableIdPart(text)}`);
  let tooltipId = $derived(id ?? _fallbackId);

  let visible = $state(false);

  // Under compact density, non-critical tooltips are suppressed entirely.
  let suppressed = $derived(
    severity !== 'critical' && uiPreferences.helpDensity === 'compact'
  );

  function show() { visible = true; }
  function hide() { visible = false; }

  function handleKeydown(event) {
    if (event.key === 'Escape') {
      visible = false;
    }
  }
</script>

{#if severity === 'critical'}
  <!-- Critical: always visible inline regardless of density. -->
  <span
    role="alert"
    class="help-tooltip help-tooltip--critical"
    aria-live="assertive"
  >{text}</span>
{:else if !suppressed}
  <!-- Non-critical: trigger button reveals tooltip on focus/hover. -->
  <span class="help-tooltip-wrap">
    <button
      type="button"
      class="help-tooltip-trigger"
      aria-describedby={tooltipId}
      onfocus={show}
      onblur={hide}
      onmouseenter={show}
      onmouseleave={hide}
      onkeydown={handleKeydown}
      aria-label="Help: {text}"
    >
      <span aria-hidden="true" class="help-icon">?</span>
    </button>
    <div
      id={tooltipId}
      role="tooltip"
      class="help-tooltip-bubble"
      class:help-tooltip-bubble--visible={visible}
      aria-hidden={!visible}
    >{text}</div>
  </span>
{/if}

<style>
  .help-tooltip--critical {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 4px 10px;
    border-radius: var(--radius-sm, 4px);
    background: #5f1f28;
    color: #fff5f5;
    font-size: 0.8125rem;
    font-weight: 500;
    border: 1px solid rgba(255, 154, 154, 0.55);
  }

  .help-tooltip-wrap {
    position: relative;
    display: inline-flex;
    align-items: center;
  }

  .help-tooltip-trigger {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 18px;
    height: 18px;
    border-radius: 50%;
    border: 1px solid var(--border-default, #ccc);
    background: var(--surface-hover, #f5f5f5);
    color: var(--text-muted, #888);
    font-size: 0.6875rem;
    font-weight: 700;
    cursor: pointer;
    font-family: inherit;
    padding: 0;
    /* Visible focus indicator — never suppressed */
    outline: 2px solid transparent;
    outline-offset: 2px;
  }

  .help-tooltip-trigger:focus-visible {
    outline: 2px solid var(--primary, #4a90e2);
    outline-offset: 2px;
  }

  .help-icon { line-height: 1; pointer-events: none; }

  .help-tooltip-bubble {
    position: absolute;
    bottom: calc(100% + 6px);
    left: 50%;
    transform: translateX(-50%);
    min-width: 180px;
    max-width: 280px;
    padding: 6px 10px;
    border-radius: var(--radius-sm, 4px);
    background: var(--surface-elevated, #2a2a2a);
    color: var(--text-primary, #f0f0f0);
    font-size: 0.8125rem;
    line-height: 1.4;
    white-space: normal;
    word-break: break-word;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.25);
    z-index: 100;
    /* Hidden by default — shown via class toggle rather than display:none so
       focus events still work without layout shifts breaking aria links. */
    opacity: 0;
    pointer-events: none;
    transition: opacity var(--transition-base, 120ms ease);
  }

  /* Respect prefers-reduced-motion */
  @media (prefers-reduced-motion: reduce) {
    .help-tooltip-bubble {
      transition: none;
    }
  }

  .help-tooltip-bubble--visible {
    opacity: 1;
    pointer-events: auto;
  }
</style>
