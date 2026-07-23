<script>
  import { requireEvidence } from '$lib/evidence/evidenceGuard.js';

  const { signals = [], selectedSeverity = 'all', onSelect = undefined } = $props();

  let activeSeverity = $state(selectedSeverity);
  $effect(() => {
    activeSeverity = selectedSeverity;
  });

  function evidenceIssue(signal) {
    const refs = Array.isArray(signal?.evidence_refs) ? signal.evidence_refs : [];
    if (refs.length === 0) {
      return 'missing_evidence_refs';
    }
    try {
      requireEvidence(refs, `monitoring-signal:${signal?.signal_id ?? 'unknown'}`);
      return '';
    } catch (error) {
      return error.message;
    }
  }

  const normalizedSignals = $derived(
    signals.map((signal) => {
      const issue = evidenceIssue(signal);
      return {
        ...signal,
        severity: signal.severity ?? 'info',
        kind: signal.kind ?? 'unknown',
        degraded: Boolean(signal.degraded) || Boolean(issue),
        alerting: Boolean(signal.alerting),
        evidenceIssue: issue,
      };
    }),
  );

  const severityOrder = ['critical', 'error', 'warning', 'info'];
  const visibleSignals = $derived(
    activeSeverity === 'all'
      ? normalizedSignals
      : normalizedSignals.filter((signal) => signal.severity === activeSeverity),
  );
  const counts = $derived(
    severityOrder.map((severity) => ({
      severity,
      count: normalizedSignals.filter((signal) => signal.severity === severity).length,
    })),
  );

  function selectSignal(signal) {
    if (signal.evidenceIssue) {
      return;
    }
    if (typeof onSelect === 'function') {
      onSelect(signal);
    }
  }

  function statusLabel(signal) {
    if (signal.degraded) {
      return signal.evidenceIssue || 'Degraded';
    }
    if (signal.alerting) {
      return 'Alerting';
    }
    return 'Healthy';
  }
</script>

<section class="monitoring-signal-panel" aria-label="Production AI monitoring signals">
  <div class="severity-tabs" role="tablist" aria-label="Signal severity filter">
    <button type="button" class:active={activeSeverity === 'all'} onclick={() => (activeSeverity = 'all')}>
      All
    </button>
    {#each counts as item (item.severity)}
      <button
        type="button"
        class:active={activeSeverity === item.severity}
        onclick={() => (activeSeverity = item.severity)}
      >
        {item.severity} <span>{item.count}</span>
      </button>
    {/each}
  </div>

  {#if visibleSignals.length === 0}
    <p class="empty-state" role="status">No monitoring signals.</p>
  {:else}
    <div class="signal-grid">
      {#each visibleSignals as signal (signal.signal_id)}
        <article class:degraded={signal.degraded} class:alerting={signal.alerting} class="signal-row">
          <div class="signal-main">
            <h3>{signal.kind}</h3>
            <p>{signal.signal_id}</p>
          </div>
          <dl class="signal-facts">
            <div>
              <dt>Status</dt>
              <dd>{statusLabel(signal)}</dd>
            </div>
            <div>
              <dt>Score</dt>
              <dd>{signal.score ?? 'n/a'}</dd>
            </div>
            <div>
              <dt>Evidence</dt>
              <dd>{signal.evidence_refs?.length ?? 0}</dd>
            </div>
          </dl>
          <button type="button" class="inspect-button" onclick={() => selectSignal(signal)}>Inspect</button>
        </article>
      {/each}
    </div>
  {/if}
</section>

<style>
  .monitoring-signal-panel {
    width: 100%;
  }

  .severity-tabs {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-bottom: 10px;
  }

  .severity-tabs button {
    min-height: 32px;
    border: 1px solid var(--border-default, #d6d9de);
    border-radius: 6px;
    background: var(--surface-secondary, #f8fafc);
    color: var(--text-primary, #111827);
    font: inherit;
    font-size: 0.75rem;
    font-weight: 700;
    cursor: pointer;
  }

  .severity-tabs button.active {
    border-color: var(--primary, #2563eb);
    background: var(--primary-soft, rgba(37, 99, 235, 0.12));
  }

  .severity-tabs span {
    margin-left: 4px;
    color: var(--text-muted, #6b7280);
  }

  .signal-grid {
    display: grid;
    gap: 8px;
  }

  .signal-row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto 88px;
    align-items: center;
    gap: 12px;
    min-height: 76px;
    padding: 12px;
    border: 1px solid var(--border-default, #d6d9de);
    border-radius: 6px;
    background: var(--surface-primary, #fff);
  }

  .signal-row.alerting {
    border-color: var(--warning, #b45309);
    box-shadow: inset 3px 0 0 var(--warning, #b45309);
  }

  .signal-row.degraded {
    border-color: var(--danger, #dc2626);
    box-shadow: inset 3px 0 0 var(--danger, #dc2626);
  }

  .signal-main {
    min-width: 0;
  }

  .signal-main h3 {
    margin: 0 0 4px;
    color: var(--text-primary, #111827);
    font-size: 0.9375rem;
    font-weight: 700;
  }

  .signal-main p {
    margin: 0;
    color: var(--text-muted, #4b5563);
    font-size: 0.8125rem;
    line-height: 1.4;
    overflow-wrap: anywhere;
  }

  .signal-facts {
    display: grid;
    grid-template-columns: repeat(3, minmax(58px, auto));
    gap: 8px;
    margin: 0;
  }

  .signal-facts dt {
    color: var(--text-muted, #6b7280);
    font-size: 0.6875rem;
    line-height: 1.2;
  }

  .signal-facts dd {
    margin: 2px 0 0;
    color: var(--text-primary, #111827);
    font-size: 0.8125rem;
    font-weight: 700;
  }

  .inspect-button {
    width: 88px;
    min-height: 36px;
    border: 1px solid var(--border-default, #cbd5e1);
    border-radius: 6px;
    background: var(--surface-secondary, #f8fafc);
    color: var(--text-primary, #111827);
    font: inherit;
    font-size: 0.8125rem;
    font-weight: 700;
    cursor: pointer;
  }

  .inspect-button:hover,
  .inspect-button:focus-visible,
  .severity-tabs button:focus-visible {
    border-color: var(--primary, #2563eb);
    outline: 2px solid var(--primary-soft, rgba(37, 99, 235, 0.22));
    outline-offset: 2px;
  }

  .empty-state {
    margin: 0;
    padding: 12px;
    border: 1px solid var(--border-default, #d6d9de);
    border-radius: 6px;
    color: var(--text-muted, #4b5563);
    font-size: 0.875rem;
  }

  @media (max-width: 720px) {
    .signal-row {
      grid-template-columns: 1fr;
      align-items: stretch;
    }

    .signal-facts {
      grid-template-columns: repeat(3, minmax(0, 1fr));
    }

    .inspect-button {
      width: 100%;
    }
  }
</style>
