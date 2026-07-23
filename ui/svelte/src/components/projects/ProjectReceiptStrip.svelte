<script>
  /**
   * Per-project receipt strip — compact summary of WorkReceipt counts by
   * kind for one project. Reads only real receipts from
   * /api/projects/<project_id>/receipts; never synthesises a status when
   * a project has zero receipts. Empty-state copy is "No work recorded
   * yet" (anti-pattern: Fallback as success).
   *
   * Used inside Project cards on the Projects view to give an at-a-glance
   * answer to "what work has happened on this project?" without having
   * to open the project detail.
   */
  import * as api from '$lib/api.js';
  import Icon from '$lib/a11y/Icon.svelte';

  /** @type {{ projectId: string }} */
  let { projectId } = $props();

  /** Counts by WorkReceiptKind. Initialised at zero so the template never
   *  has to guard for ``undefined``. */
  let counts = $state({
    plan_round: 0,
    worker_task: 0,
    inspector_pass: 0,
    training_step: 0,
    release_step: 0,
  });
  let total = $state(0);
  let loading = $state(true);
  let error = $state(null);
  let errorId = $derived(projectId ? `receipt-strip-error-${projectId}` : 'receipt-strip-error');

  const KINDS = [
    { key: 'plan_round', label: 'plan', icon: 'fa-clipboard-list' },
    { key: 'worker_task', label: 'work', icon: 'fa-cog' },
    { key: 'inspector_pass', label: 'inspect', icon: 'fa-magnifying-glass' },
    { key: 'training_step', label: 'train', icon: 'fa-brain' },
    { key: 'release_step', label: 'release', icon: 'fa-rocket' },
  ];

  async function fetchCounts() {
    if (!projectId) {
      loading = false;
      return;
    }
    try {
      // Pull up to 500 receipts in one call. For projects with more than
      // that the strip is approximate but the total field still reflects
      // the true receipt count from the API.
      const data = await api.listProjectReceipts(projectId, { limit: 500 });
      const fresh = {
        plan_round: 0,
        worker_task: 0,
        inspector_pass: 0,
        training_step: 0,
        release_step: 0,
      };
      for (const r of data?.receipts ?? []) {
        if (r.kind in fresh) fresh[r.kind] += 1;
      }
      counts = fresh;
      total = data?.total ?? 0;
      error = null;
    } catch (err) {
      error = err.message;
    } finally {
      loading = false;
    }
  }

  $effect(() => {
    fetchCounts();
  });
</script>

<div class="receipt-strip" role="status" aria-live="polite" aria-label="Work receipts for {projectId}">
  {#if error}
    <span class="strip-error" role="alert" aria-live="assertive" aria-describedby={errorId}>
      <Icon name="exclamation-triangle" />
      receipts unavailable
      <span id={errorId}><span class="sr-only">{error}</span></span>
    </span>
  {:else if loading}
    <span class="strip-muted">loading…</span>
  {:else if total === 0}
    <span class="strip-empty">No work recorded yet</span>
  {:else}
    {#each KINDS as kind (kind.key)}
      {#if counts[kind.key] > 0}
        <span class="strip-chip" aria-label="{counts[kind.key]} {kind.label} receipt{counts[kind.key] === 1 ? '' : 's'}">
          <i class="fas {kind.icon}" aria-hidden="true"></i>
          <span class="chip-count">{counts[kind.key]}</span>
          <span class="chip-label">{kind.label}</span>
        </span>
      {/if}
    {/each}
  {/if}
</div>

<style>
  .receipt-strip {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    align-items: center;
    margin-top: 4px;
    font-size: 0.75rem;
  }

  .strip-empty,
  .strip-muted {
    color: var(--text-muted);
    font-style: italic;
  }

  .strip-error {
    color: var(--danger);
    display: inline-flex;
    align-items: center;
    gap: 4px;
  }

  .strip-chip {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 2px 8px;
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid var(--border-default);
    border-radius: 999px;
    color: var(--text-primary);
    font-variant-numeric: tabular-nums;
  }

  .chip-count {
    font-weight: 600;
  }

  .chip-label {
    color: var(--text-muted);
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
