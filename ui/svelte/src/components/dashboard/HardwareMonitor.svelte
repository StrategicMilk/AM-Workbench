<script>
  /**
   * Hardware resource usage display with accessible progress bars.
   *
   * Shows GPU, CPU, RAM, and Disk utilization as horizontal bars with
   * numeric values and ARIA progressbar semantics.
   *
   * @prop {object} hardware - Object with gpu, cpu, ram, disk percent values.
   */
  let { hardware = {} } = $props();

  /** Map resource keys to display labels and colors. */
  const RESOURCES = [
    { key: 'gpu', label: 'GPU', color: 'var(--secondary)' },
    { key: 'cpu', label: 'CPU', color: 'var(--primary)' },
    { key: 'ram', label: 'RAM', color: 'var(--warning)' },
    { key: 'disk', label: 'Disk', color: 'var(--success)' },
  ];

  /**
   * Parse a resource value from the backend into a 0–100 number, or null when
   * the value is absent or non-numeric (e.g. "N/A", "--", null, undefined).
   *
   * Returning null rather than 0 lets the template distinguish "not available"
   * from genuine zero usage so it can show "—" instead of "0%" and omit the
   * invalid aria-valuenow attribute.
   *
   * @param {string} key - Resource key in the `hardware` prop object.
   * @returns {number|null}
   */
  function getUsage(key) {
    const val = hardware[key];
    if (val == null) return null;
    const n = +val;
    // +val produces NaN for strings like "N/A", "--%", ""
    if (Number.isNaN(n)) return null;
    return Math.min(100, Math.max(0, n));
  }

  /**
   * Build the inline style for the bar fill, using 0% width for null usage
   * so the bar renders empty (invisible) rather than broken.
   *
   * @param {number|null} usage - Parsed usage from getUsage(), or null.
   * @param {string} color - CSS color value for this resource.
   * @returns {string}
   */
  function barStyle(usage, color) {
    const pct = usage ?? 0;
    return `width: ${pct}%; background: ${color}`;
  }
</script>

<div class="hardware-monitor">
  <h3 class="hardware-title">
    <i class="fas fa-server"></i>
    System Resources
  </h3>

  <div class="hardware-bars">
    {#each RESOURCES as res}
      {@const usage = getUsage(res.key)}
      {@const isKnown = usage !== null}
      <div class="resource-row">
        <span class="resource-label">{res.label}</span>
        <div
          class="resource-bar"
          role="progressbar"
          aria-valuenow={isKnown ? usage : undefined}
          aria-valuemin={isKnown ? 0 : undefined}
          aria-valuemax={isKnown ? 100 : undefined}
          aria-valuetext={isKnown ? `${usage.toFixed(0)}%` : 'unavailable'}
          aria-label="{res.label} usage"
        >
          <div
            class="resource-bar-fill"
            class:high={isKnown && usage > 80}
            style={barStyle(usage, res.color)}
          ></div>
        </div>
        <span class="resource-value" class:resource-value--unknown={!isKnown}>
          {isKnown ? `${usage.toFixed(0)}%` : '\u2014'}
        </span>
      </div>
    {/each}
  </div>
</div>

<style>
  .hardware-monitor {
    padding: 20px;
    background: var(--surface-bg);
    border: 1px solid var(--border-default);
    border-radius: 12px;
  }

  .hardware-title {
    font-size: 0.9375rem;
    font-weight: 600;
    color: var(--text-primary);
    margin: 0 0 16px 0;
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .hardware-title i {
    color: var(--text-muted);
  }

  .hardware-bars {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .resource-row {
    display: flex;
    align-items: center;
    gap: 12px;
  }

  .resource-label {
    width: 36px;
    font-size: 0.8125rem;
    font-weight: 500;
    color: var(--text-secondary);
    flex-shrink: 0;
  }

  .resource-bar {
    flex: 1;
    height: 8px;
    background: rgba(255, 255, 255, 0.06);
    border-radius: 4px;
    overflow: hidden;
  }

  .resource-bar-fill {
    height: 100%;
    border-radius: 4px;
    transition: width 300ms ease;
  }

  .resource-bar-fill.high {
    animation: pulse-bar 2s ease-in-out infinite;
  }

  .resource-value {
    width: 40px;
    text-align: right;
    font-size: 0.8125rem;
    font-weight: 500;
    color: var(--text-primary);
    font-variant-numeric: tabular-nums;
    flex-shrink: 0;
  }

  .resource-value--unknown {
    color: var(--text-muted);
  }

  @keyframes pulse-bar {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.7; }
  }
</style>
