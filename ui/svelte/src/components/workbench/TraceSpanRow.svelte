<script>
  import { clampPercent, errorMessage, finiteNumber, nonEmptyString } from '$lib/utils/safe.js';

  let { span, depth = 0 } = $props();

  let durationMs = $derived(finiteNumber(span?.duration_ms, null));
  let width = $derived(durationMs === null ? 4 : clampPercent(durationMs / 10, 4));
  let spanId = $derived(nonEmptyString(span?.span_id, 'unknown-span'));
  let toolName = $derived(nonEmptyString(span?.tool_name, 'unknown tool'));
  let durationLabel = $derived(durationMs === null ? 'missing' : `${durationMs} ms`);
  let errorLabel = $derived(span?.error ? errorMessage(span.error, 'Trace span failed.') : null);
</script>

<div class="trace-span" style="--depth: {depth}" data-testid="trace-span-{spanId}">
  <span class="bar" style="width: {width}%" aria-hidden="true"></span>
  <span class="name">{toolName}</span>
  <span class:missing={durationMs === null} class="duration">{durationLabel}</span>
  {#if errorLabel}
    <span class="error">{errorLabel}</span>
  {/if}
</div>

<style>
  .trace-span { position: relative; display: grid; grid-template-columns: 1fr auto; gap: 8px; margin-left: calc(var(--depth) * 18px); padding: 7px 8px; border: 1px solid var(--border-subtle); border-radius: 6px; overflow: hidden; background: var(--surface-bg); }
  .bar { position: absolute; inset: 0 auto 0 0; background: var(--primary-muted); z-index: 0; }
  .name, .duration, .error { position: relative; z-index: 1; font-size: 0.78rem; }
  .name { color: var(--text-primary); font-weight: 600; }
  .duration { font-family: var(--font-mono); color: var(--text-muted); }
  .duration.missing { color: var(--warning); }
  .error { grid-column: 1 / -1; color: var(--danger); }
</style>
