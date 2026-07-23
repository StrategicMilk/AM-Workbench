<script>
  /**
   * Chart.js wrapper component for dashboard timeseries charts.
   *
   * Creates and manages a Chart.js instance using design tokens for styling.
   * Automatically destroys the chart on component teardown and updates
   * reactively when data changes.
   *
   * @prop {string} title - Chart heading text.
   * @prop {'line'|'bar'|'doughnut'} [type='line'] - Chart.js chart type.
   * @prop {object} data - Chart.js data configuration.
   * @prop {object} [options={}] - Chart.js options override.
   */
  import { Chart, registerables } from 'chart.js';
  import { getChartDefaults } from '$lib/tokens.js';
  import ProvenanceGate from '$lib/security/ProvenanceGate.svelte';

  // Register all Chart.js components once
  Chart.register(...registerables);

  let { title, type = 'line', data, options = {} } = $props();

  let canvasEl;
  let chartInstance = null;
  let chartTableId = $derived(`chart-data-${String(title ?? 'metrics').toLowerCase().replace(/[^a-z0-9]+/g, '-')}`);
  let chartSummary = $derived(
    `${title || 'Metrics chart'}: ${(data?.datasets ?? [])
      .map((dataset) => `${dataset.label || 'dataset'} ${dataset.data?.join(', ') || 'no values'}`)
      .join('; ') || 'no data'}`
  );
  const chartProvenanceRefs = [
    'npm:chart.js@4.5.1',
    'ui/svelte/package-lock.json#node_modules/chart.js',
  ];

  /**
   * Build merged Chart.js options from token defaults and user overrides.
   *
   * @param {string} chartType - The chart type ('line', 'bar', 'doughnut').
   * @returns {object} Merged options object ready for Chart.js.
   */
  function buildOptions(chartType) {
    const defaults = getChartDefaults(chartType);
    return {
      ...defaults.options,
      ...options,
      plugins: { ...defaults.options.plugins, ...options.plugins },
      scales: chartType === 'doughnut'
        ? {}
        : { ...defaults.options.scales, ...options.scales },
    };
  }

  /**
   * Create or update the Chart.js instance.
   *
   * When the `type` prop changes (e.g. 'line' → 'bar') the existing chart
   * instance is destroyed before creating a new one. Chart.js 4.x does allow
   * mutating config.type, but it requires separate scale registration and
   * re-initialisation that is error-prone. Destroy+recreate is the documented
   * safe path for type transitions.
   *
   * When only data or options change, the instance is updated in-place to
   * avoid the canvas flicker a full recreate would cause.
   */
  function renderChart() {
    if (!canvasEl || !data) return;

    const mergedOptions = buildOptions(type);

    // Destroy the existing instance when the chart type has changed so the
    // canvas is re-initialised with correct scale and element registrations.
    if (chartInstance && chartInstance.config.type !== type) {
      chartInstance.destroy();
      chartInstance = null;
    }

    if (chartInstance) {
      chartInstance.data = data;
      chartInstance.options = mergedOptions;
      chartInstance.update('none');
    } else {
      chartInstance = new Chart(canvasEl, {
        type,
        data,
        options: mergedOptions,
      });
    }
  }

  // Reactively re-render when data, options, or type changes.
  // All three are referenced explicitly so Svelte tracks each as a dependency.
  $effect(() => {
    void data;
    void options;
    void type;
    renderChart();
  });

  // Clean up on destroy
  $effect(() => {
    return () => {
      if (chartInstance) {
        chartInstance.destroy();
        chartInstance = null;
      }
    };
  });
</script>

<div class="metrics-chart">
  {#if title}
    <h3 class="chart-title">{title}</h3>
  {/if}
  <ProvenanceGate refs={chartProvenanceRefs} status="verified" context="dashboard-chartjs-bundle" compact />
  <div class="chart-container" role="img" aria-label={chartSummary} aria-describedby={chartTableId}>
    <canvas bind:this={canvasEl} aria-hidden="true"></canvas>
  </div>
  <table id={chartTableId} class="sr-only">
    <caption>{title || 'Metrics chart'} data</caption>
    <tbody>
      {#each data?.datasets ?? [] as dataset}
        <tr>
          <th scope="row">{dataset.label || 'dataset'}</th>
          <td>{dataset.data?.join(', ') || 'no values'}</td>
        </tr>
      {/each}
    </tbody>
  </table>
</div>

<style>
  .metrics-chart {
    padding: 20px;
    background: var(--surface-bg);
    border: 1px solid var(--border-default);
    border-radius: 12px;
  }

  .chart-title {
    font-size: 0.9375rem;
    font-weight: 600;
    color: var(--text-primary);
    margin: 0 0 12px 0;
  }

  .chart-container {
    position: relative;
    height: 200px;
  }

  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
    border: 0;
  }
</style>
