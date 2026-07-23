<script>
  import {
    WORKBENCH_PRESSURE,
    WORKBENCH_PRESSURE_LABELS,
    WORKBENCH_QUEUE_LANE_LABELS,
    WORKBENCH_QUEUE_LANES,
  } from '$lib/uiEnums.js';

  /** Three-lane pressure board for Workbench Mission Control. */
  let { lanes = [] } = $props();

  function laneByName(name) {
    return lanes.find((lane) => lane.lane === name) ?? {
      lane: name,
      active_count: 0,
      queued_count: 0,
      vram_share_committed: 0,
      vram_share_observed: 0,
      pressure: WORKBENCH_PRESSURE.GREEN,
      missing: true,
    };
  }

  function percent(value) {
    return `${Math.max(0, Math.min(100, Number(value ?? 0) * 100)).toFixed(0)}%`;
  }

  function meterValue(value) {
    return Math.max(0, Math.min(100, Number(value ?? 0) * 100));
  }
</script>

<div class="lane-board" aria-label="Scheduler lane pressure">
  {#each WORKBENCH_QUEUE_LANES as laneName (laneName)}
    {@const lane = laneByName(laneName)}
    <section class="lane-tile" role="group" aria-labelledby="lane-{laneName}-heading" data-lane={laneName}>
      <div class="lane-heading">
        <h3 id="lane-{laneName}-heading">{WORKBENCH_QUEUE_LANE_LABELS[laneName]}</h3>
        <span class="pressure-chip pressure-{lane.pressure}">
          {WORKBENCH_PRESSURE_LABELS[lane.pressure] ?? lane.pressure}
        </span>
      </div>

      {#if lane.missing}
        <p class="missing">(no data)</p>
      {/if}

      <dl class="lane-counts">
        <div>
          <dt>Active</dt>
          <dd>{lane.active_count}</dd>
        </div>
        <div>
          <dt>Queued</dt>
          <dd>{lane.queued_count}</dd>
        </div>
      </dl>

      <div
        class="vram-meter"
        role="meter"
        aria-label={`${WORKBENCH_QUEUE_LANE_LABELS[laneName]} committed VRAM share`}
        aria-valuemin="0"
        aria-valuemax="100"
        aria-valuenow={meterValue(lane.vram_share_committed)}
        aria-valuetext={`Committed ${percent(lane.vram_share_committed)}, observed ${percent(lane.vram_share_observed)}`}
      >
        <span class="bar committed" style:width={percent(lane.vram_share_committed)} aria-hidden="true"></span>
        <span class="bar observed" style:width={percent(lane.vram_share_observed)} aria-hidden="true"></span>
      </div>
      <p class="sr-only" role="status" aria-live="polite">
        {WORKBENCH_QUEUE_LANE_LABELS[laneName]} VRAM committed {percent(lane.vram_share_committed)}, observed {percent(lane.vram_share_observed)}.
      </p>
      <div class="vram-labels">
        <span>Committed {percent(lane.vram_share_committed)}</span>
        <span>Observed {percent(lane.vram_share_observed)}</span>
      </div>
    </section>
  {/each}
</div>

<style>
  .lane-board {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 12px;
  }

  .lane-tile {
    border: 1px solid var(--border-default);
    border-radius: 8px;
    padding: 12px;
    background: var(--surface-elevated);
    min-width: 0;
  }

  .lane-heading,
  .vram-labels {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }

  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
  }

  h3 {
    margin: 0;
    font-size: 0.95rem;
  }

  .pressure-chip {
    border-radius: 999px;
    padding: 2px 8px;
    font-size: 0.75rem;
    font-weight: 700;
  }

  .pressure-green {
    border: 1px solid var(--success);
    background: var(--success-muted);
    color: var(--success);
  }

  .pressure-amber {
    border: 1px solid var(--warning);
    background: var(--warning-muted);
    color: var(--warning);
  }

  .pressure-red {
    border: 1px solid var(--danger);
    background: var(--danger-muted);
    color: var(--danger);
  }

  .missing {
    color: var(--text-muted);
    margin: 8px 0 0;
  }

  .lane-counts {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 8px;
    margin: 12px 0;
  }

  .lane-counts div {
    border-radius: 6px;
    background: rgba(148, 163, 184, 0.12);
    padding: 8px;
  }

  dt {
    color: var(--text-muted, #94a3b8);
    font-size: 0.75rem;
  }

  dd {
    margin: 0;
    font-size: 1.2rem;
    font-weight: 700;
  }

  .vram-meter {
    position: relative;
    height: 10px;
    overflow: hidden;
    border-radius: 999px;
    background: rgba(148, 163, 184, 0.18);
  }

  .bar {
    position: absolute;
    inset-block: 0;
    left: 0;
    border-radius: inherit;
  }

  .committed {
    background: rgba(59, 130, 246, 0.35);
  }

  .observed {
    background: rgba(20, 184, 166, 0.75);
  }

  .vram-labels {
    margin-top: 6px;
    color: var(--text-muted, #94a3b8);
    font-size: 0.72rem;
  }

  @media (max-width: 900px) {
    .lane-board {
      grid-template-columns: 1fr;
    }
  }
</style>
