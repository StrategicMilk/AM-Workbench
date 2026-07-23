<script>
  /**
   * Task progress section showing stage indicators, ETA, and progress bar.
   *
   * Displays the current pipeline stage (Foreman -> Worker -> Inspector),
   * overall progress percentage, and estimated time remaining.
   *
   * @prop {object|null} project - Current project data with progress info.
   */
  import { percent, duration } from '$lib/utils/format.js';
  import { asArray, clampPercent } from '$lib/utils/safe.js';

  let { project = null } = $props();

  const STAGES = [
    { id: 'planning', label: 'Planning', icon: 'fas fa-map' },
    { id: 'executing', label: 'Executing', icon: 'fas fa-hammer' },
    { id: 'reviewing', label: 'Reviewing', icon: 'fas fa-search' },
    { id: 'complete', label: 'Complete', icon: 'fas fa-check' },
  ];

  // The backend project response exposes explicit stage/progress/eta_ms fields
  // for current projects. We still derive fallbacks from tasks/status so older
  // cached payloads do not render stale optimistic values or NaN/undefined.
  //
  // stage - explicit backend field or inferred from project.config.status.
  // progress - explicit backend field or computed from tasks when available.
  // eta_ms - explicit backend field; null hides the ETA row.

  /** Map backend status strings onto the STAGES ids. */
  function statusToStage(status) {
    if (!status) return null;
    const s = status.toLowerCase();
    if (s === 'completed' || s === 'complete' || s === 'archived') return 'complete';
    if (s === 'running' || s === 'executing' || s === 'in_progress') return 'executing';
    if (s === 'reviewing') return 'reviewing';
    if (s === 'planned' || s === 'pending' || s === 'paused') return 'planning';
    return null;
  }

  // $derived.by(() => ...) is required for multi-statement derivations in Svelte 5.
  // $derived(() => ...) stores the arrow function itself as the value — it does NOT
  // call it.  That means currentStage and progress would be functions, not values,
  // so stageIndex would always be -1 and the progress bar would receive a function
  // instead of a number.
  let currentStage = $derived.by(() => {
    // Prefer the explicit project stage returned by the backend.
    if (project?.stage) return project.stage;
    // Derive from status fields that ARE in the backend payload.
    const status = project?.config?.status ?? project?.status ?? null;
    return statusToStage(status) ?? 'planning';
  });

  let progress = $derived.by(() => {
    // Prefer the explicit project progress returned by the backend.
    if (typeof project?.progress === 'number') {
      return clampPercent(project.progress, null);
    }
    // Derive from tasks array: completed / total.
    const tasks = asArray(project?.tasks);
    if (tasks.length > 0) {
      const done = tasks.filter(
        (t) => t.status === 'completed' || t.status === 'complete',
      ).length;
      return Math.round((done / tasks.length) * 100);
    }
    // No reliable data — return null so the progress bar is hidden.
    return null;
  });

  let activeTask = $derived.by(() => {
    const tasks = asArray(project?.tasks);
    if (tasks.length === 0) return null;
    return (
      tasks.find((task) => task.status === 'running' || task.status === 'in_progress') ??
      tasks.find((task) => task.status === 'pending' || task.status === 'planned') ??
      tasks[tasks.length - 1]
    );
  });

  let eta = $derived(project?.eta_ms ?? null);

  let stageIndex = $derived(
    STAGES.findIndex((s) => s.id === currentStage)
  );
</script>

{#if project}
  <div class="progress-section">
    <!-- Stage indicators (Foreman -> Worker -> Inspector pipeline) -->
    <div class="stage-pipeline">
      {#each STAGES as stage, i}
        <div
          class="stage"
          class:active={i === stageIndex}
          class:completed={i < stageIndex}
        >
          <div class="stage-icon">
            {#if i < stageIndex}
              <i class="fas fa-check"></i>
            {:else}
              <i class={stage.icon}></i>
            {/if}
          </div>
          <span class="stage-label">{stage.label}</span>
        </div>
        {#if i < STAGES.length - 1}
          <div class="stage-connector" class:filled={i < stageIndex}></div>
        {/if}
      {/each}
    </div>

    <!-- Progress bar — only rendered when a meaningful value is available.
         progress is null when neither explicit backend field nor tasks array
         can supply a real number, preventing NaN/undefined in the UI. -->
    {#if progress !== null}
      <div class="progress-track">
        <div
          class="progress-bar-outer"
          role="progressbar"
          aria-valuenow={progress}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-label="Task progress: {percent(progress, 0)}"
        >
          <div
            class="progress-bar-fill"
            style="width: {progress}%"
            class:complete={progress >= 100}
          ></div>
        </div>
      <div class="progress-meta">
        <span>{percent(progress, 0)}</span>
        {#if activeTask}
          <span>{activeTask.task_index ?? '?'} / {activeTask.total_tasks ?? project?.tasks?.length ?? '?'}: {activeTask.description ?? activeTask.id}</span>
        {/if}
        {#if eta}
          <span>ETA: {duration(eta)}</span>
          {/if}
        </div>
      </div>
    {:else}
      <!-- No reliable progress value is available yet. -->
      <div class="progress-meta">
        <span class="progress-unknown">Running</span>
        {#if activeTask}
          <span>{activeTask.task_index ?? '?'} / {activeTask.total_tasks ?? project?.tasks?.length ?? '?'}: {activeTask.description ?? activeTask.id}</span>
        {/if}
      </div>
    {/if}
  </div>
{/if}

<style>
  .progress-section {
    padding: 16px 20px;
    background: var(--surface-bg);
    border: 1px solid var(--border-default);
    border-radius: 10px;
    margin-bottom: 16px;
  }

  .stage-pipeline {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 0;
    margin-bottom: 16px;
  }

  .stage {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 4px;
    opacity: 0.4;
    transition: opacity 200ms;
  }

  .stage.active {
    opacity: 1;
  }

  .stage.completed {
    opacity: 0.8;
  }

  .stage-icon {
    width: 36px;
    height: 36px;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 14px;
    background: rgba(255, 255, 255, 0.06);
    color: var(--text-muted);
    transition: all 200ms;
  }

  .stage.active .stage-icon {
    background: var(--primary);
    color: white;
  }

  .stage.completed .stage-icon {
    background: var(--success);
    color: white;
  }

  .stage-label {
    font-size: 0.6875rem;
    color: var(--text-muted);
  }

  .stage.active .stage-label {
    color: var(--text-primary);
    font-weight: 500;
  }

  .stage-connector {
    width: 40px;
    height: 2px;
    background: rgba(255, 255, 255, 0.08);
    margin: 0 8px;
    margin-bottom: 20px;
    transition: background 200ms;
  }

  .stage-connector.filled {
    background: var(--success);
  }

  .progress-track {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .progress-bar-outer {
    height: 6px;
    background: rgba(255, 255, 255, 0.06);
    border-radius: 3px;
    overflow: hidden;
  }

  .progress-bar-fill {
    height: 100%;
    background: var(--primary);
    border-radius: 3px;
    transition: width 500ms ease;
  }

  .progress-bar-fill.complete {
    background: var(--success);
  }

  .progress-meta {
    display: flex;
    justify-content: space-between;
    font-size: 0.75rem;
    color: var(--text-muted);
  }

  .progress-unknown {
    font-size: 0.75rem;
    color: var(--text-muted);
    font-style: italic;
  }
</style>
