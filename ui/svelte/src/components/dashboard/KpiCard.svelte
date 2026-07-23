<script>
  /**
   * Reusable KPI metric card for the dashboard.
   *
   * Displays an icon, formatted value, label, and optional trend indicator
   * using design tokens for theming.
   *
   * @prop {string} icon - Font Awesome class (e.g. "fas fa-tasks").
   * @prop {string} value - Pre-formatted display value.
   * @prop {string} label - Metric label text.
   * @prop {string} [color='primary'] - Token color name for icon accent.
   * @prop {number|null} [trend=null] - Percent change; positive = up, negative = down.
   */
  let { icon, value, label, color = 'primary', trend = null } = $props();

  /**
   * Normalise the trend prop to a finite number or null.
   *
   * Non-null non-finite values (NaN, ±Infinity) and non-number types
   * (e.g. "N/A", undefined that slipped through) are coerced to null so
   * the trend block is hidden instead of rendering "NaN%".
   */
  let safeTrend = $derived(
    trend != null && typeof trend === 'number' && Number.isFinite(trend)
      ? trend
      : null
  );

  let trendClass = $derived(
    safeTrend == null ? '' :
    safeTrend > 0 ? 'trend-up' :
    safeTrend < 0 ? 'trend-down' : 'trend-flat'
  );

  let trendIcon = $derived(
    safeTrend == null ? '' :
    safeTrend > 0 ? 'fa-arrow-up' :
    safeTrend < 0 ? 'fa-arrow-down' : 'fa-minus'
  );

  let trendLabel = $derived(
    safeTrend == null ? '' :
    `, trend ${safeTrend > 0 ? 'up' : safeTrend < 0 ? 'down' : 'flat'} ${Math.abs(safeTrend).toFixed(1)}%`
  );
</script>

<div class="kpi-card" role="group" aria-label={`${label}: ${value}${trendLabel}`}>
  <div class="kpi-icon" style="color: var(--{color});">
    <i class={icon} aria-hidden="true"></i>
  </div>
  <div class="kpi-body">
    <div class="kpi-value">{value}</div>
    <div class="kpi-label">{label}</div>
  </div>
  {#if safeTrend != null}
    <div class="kpi-trend {trendClass}" role="status" aria-label={trendLabel.trim()}>
      <i class="fas {trendIcon}" aria-hidden="true"></i>
      <span>{Math.abs(safeTrend).toFixed(1)}%</span>
    </div>
  {/if}
</div>

<style>
  .kpi-card {
    display: flex;
    align-items: center;
    gap: 16px;
    padding: 20px;
    background: var(--surface-bg);
    border: 1px solid var(--border-default);
    border-radius: 12px;
    transition: border-color 200ms;
  }

  .kpi-card:hover {
    border-color: var(--border-strong);
  }

  .kpi-icon {
    font-size: 24px;
    width: 48px;
    height: 48px;
    display: flex;
    align-items: center;
    justify-content: center;
    border-radius: 10px;
    background: var(--surface-hover);
    flex-shrink: 0;
  }

  .kpi-body {
    flex: 1;
    min-width: 0;
  }

  .kpi-value {
    font-size: 1.5rem;
    font-weight: 600;
    color: var(--text-primary);
    line-height: 1.2;
  }

  .kpi-label {
    font-size: 0.8125rem;
    color: var(--text-muted);
    margin-top: 2px;
  }

  .kpi-trend {
    display: flex;
    align-items: center;
    gap: 4px;
    font-size: 0.75rem;
    font-weight: 500;
    padding: 4px 8px;
    border-radius: 6px;
  }

  .trend-up {
    color: var(--success);
    background: var(--success-muted);
  }

  .trend-down {
    color: var(--danger);
    background: var(--danger-muted);
  }

  .trend-flat {
    color: var(--text-muted);
    background: var(--surface-hover);
  }
</style>
