<script>
  import { asArray } from '$lib/utils/safe.js';
  import TraceSpanRow from './TraceSpanRow.svelte';

  let { traces = [] } = $props();
  let safeTraces = $derived(asArray(traces));
  let spans = $derived(
    asArray(safeTraces).flatMap((trace) =>
      asArray(trace?.spans).map((span) => ({ ...span, trace_id: span?.trace_id ?? trace?.trace_id }))
    )
  );
  let spansByKey = $derived.by(() => new Map(spans.map((item) => [spanKey(item), item])));

  function spanKey(span) {
    return `${span?.trace_id ?? 'trace'}:${span?.span_id ?? 'span'}`;
  }

  function depthFor(span) {
    let depth = 0;
    let parent = span?.parent_span_id;
    while (parent && spansByKey.has(`${span?.trace_id ?? 'trace'}:${parent}`) && depth < 12) {
      depth += 1;
      parent = spansByKey.get(`${span?.trace_id ?? 'trace'}:${parent}`)?.parent_span_id;
    }
    return depth;
  }
</script>

<section class="trace-waterfall" data-testid={safeTraces[0]?.run_id ? `trace-waterfall-${safeTraces[0].run_id}` : 'trace-waterfall'} aria-label="Trace waterfall">
  <div class="trace-head">
    <h3>Trace waterfall</h3>
    <span>{spans.length} spans</span>
  </div>
  <div class="span-list">
    {#each spans as span, index (span?.span_id ? spanKey(span) : index)}
      <TraceSpanRow {span} depth={depthFor(span)} />
    {:else}
      <div class="empty">Select a run to load trace spans.</div>
    {/each}
  </div>
</section>

<style>
  .trace-waterfall { border: 1px solid var(--border-default); border-radius: 8px; background: var(--surface-elevated); padding: 14px; min-height: 220px; }
  .trace-head { display: flex; justify-content: space-between; gap: 10px; align-items: center; margin-bottom: 10px; }
  h3 { margin: 0; font-size: 0.95rem; color: var(--text-primary); }
  .trace-head span, .empty { color: var(--text-muted); font-size: 0.8rem; }
  .span-list { display: flex; flex-direction: column; gap: 6px; }
</style>
