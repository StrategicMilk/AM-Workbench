<script>
  /**
   * Conversational project intake flow.
   *
   * Two-stage form: first select a category from the visual grid,
   * then fill quick-config fields specific to that category.
   * Emits the completed config for project creation.
   *
   * @prop {(config: object) => void} oncreate - Called with form data when submitted.
   */

  import { IntakeDepth, IntakePriority } from '$lib/contracts/enums.js';

  let { oncreate } = $props();

  /** Category definitions matching the backend categories. */
  const CATEGORIES = [
    { id: 'code', icon: 'fas fa-code', label: 'Code', desc: 'Generate, refactor, or review code' },
    { id: 'analysis', icon: 'fas fa-chart-bar', label: 'Analysis', desc: 'Research and data analysis' },
    { id: 'writing', icon: 'fas fa-pen-fancy', label: 'Writing', desc: 'Technical or creative writing' },
    { id: 'design', icon: 'fas fa-palette', label: 'Design', desc: 'Architecture and system design' },
    { id: 'debug', icon: 'fas fa-bug', label: 'Debug', desc: 'Find and fix issues' },
    { id: 'general', icon: 'fas fa-comments', label: 'General', desc: 'Open-ended conversation' },
  ];

  /** Priority options. */
  const PRIORITIES = [
    { id: IntakePriority.LOW, label: 'Low' },
    { id: IntakePriority.MEDIUM, label: 'Normal' },
    { id: IntakePriority.HIGH, label: 'High' },
    { id: IntakePriority.CRITICAL, label: 'Critical' },
  ];

  const DEPTHS = [
    { id: IntakeDepth.QUICK, label: 'Quick' },
    { id: IntakeDepth.STANDARD, label: 'Standard' },
    { id: IntakeDepth.DEEP, label: 'Deep' },
  ];

  let selectedCategory = $state(null);
  let goal = $state('');
  let priority = $state(IntakePriority.MEDIUM);
  let technologies = $state('');
  let depth = $state(IntakeDepth.STANDARD);

  let stage = $derived(selectedCategory ? 'config' : 'category');

  function selectCategory(cat) {
    selectedCategory = cat;
  }

  function goBack() {
    selectedCategory = null;
  }

  function handleSubmit() {
    if (!goal.trim()) return;

    oncreate({
      category: selectedCategory.id,
      goal: goal.trim(),
      priority,
      technologies: technologies.split(',').map((t) => t.trim()).filter(Boolean),
      depth,
    });

    // Reset form
    selectedCategory = null;
    goal = '';
    priority = IntakePriority.MEDIUM;
    technologies = '';
    depth = IntakeDepth.STANDARD;
  }

  function moveRadioSelection(event, options, currentValue, setValue, idPrefix) {
    const currentIndex = options.findIndex((option) => option.id === currentValue);
    if (currentIndex === -1) return;

    let nextIndex = currentIndex;
    if (event.key === 'ArrowRight' || event.key === 'ArrowDown') {
      nextIndex = (currentIndex + 1) % options.length;
    } else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') {
      nextIndex = (currentIndex - 1 + options.length) % options.length;
    } else if (event.key === 'Home') {
      nextIndex = 0;
    } else if (event.key === 'End') {
      nextIndex = options.length - 1;
    } else {
      return;
    }

    event.preventDefault();
    const nextValue = options[nextIndex].id;
    setValue(nextValue);
    document.getElementById(`${idPrefix}-${nextValue}`)?.focus();
  }
</script>

<div class="intake-flow">
  {#if stage === 'category'}
    <h3 class="intake-title">What would you like to do?</h3>
    <div class="category-grid">
      {#each CATEGORIES as cat}
        <button type="button" class="category-card" onclick={() => selectCategory(cat)}>
          <i class={cat.icon} aria-hidden="true"></i>
          <span class="category-label">{cat.label}</span>
          <span class="category-desc">{cat.desc}</span>
        </button>
      {/each}
    </div>
  {:else}
    <div class="config-form">
      <div class="config-header">
        <button type="button" class="btn btn-ghost" onclick={goBack} aria-label="Back to categories">
          <i class="fas fa-arrow-left" aria-hidden="true"></i>
        </button>
        <h3>
          <i class={selectedCategory.icon} aria-hidden="true"></i>
          {selectedCategory.label}
        </h3>
      </div>

      <div class="form-group">
        <label for="intake-goal">What's your goal?</label>
        <textarea
          id="intake-goal"
          class="textarea"
          bind:value={goal}
          placeholder="Describe what you want to accomplish..."
          rows="4"
        ></textarea>
      </div>

      <div class="form-row">
        <div class="form-group">
          <label for="intake-priority">Priority</label>
          <div class="pill-selector" role="radiogroup" aria-label="Priority">
            {#each PRIORITIES as p}
              <button
                class="pill"
                class:active={priority === p.id}
                role="radio"
                aria-checked={priority === p.id}
                tabindex={priority === p.id ? 0 : -1}
                id={`intake-priority-${p.id}`}
                onclick={() => { priority = p.id; }}
                onkeydown={(event) => moveRadioSelection(event, PRIORITIES, priority, (value) => { priority = value; }, 'intake-priority')}
              >
                {p.label}
              </button>
            {/each}
          </div>
        </div>

        <div class="form-group">
          <label for="intake-depth">Depth</label>
          <div class="pill-selector" role="radiogroup" aria-label="Depth">
            {#each DEPTHS as d}
              <button
                class="pill"
                class:active={depth === d.id}
                role="radio"
                aria-checked={depth === d.id}
                tabindex={depth === d.id ? 0 : -1}
                id={`intake-depth-${d.id}`}
                onclick={() => { depth = d.id; }}
                onkeydown={(event) => moveRadioSelection(event, DEPTHS, depth, (value) => { depth = value; }, 'intake-depth')}
              >
                {d.label}
              </button>
            {/each}
          </div>
        </div>
      </div>

      <div class="form-group">
        <label for="intake-tech">Technologies (comma-separated)</label>
        <input
          id="intake-tech"
          type="text"
          class="input"
          bind:value={technologies}
          placeholder="e.g. Python, React, PostgreSQL"
        />
      </div>

      <button
        type="button"
        class="btn btn-primary submit-btn"
        onclick={handleSubmit}
        disabled={!goal.trim()}
      >
        <i class="fas fa-rocket" aria-hidden="true"></i>
        Start Project
      </button>
    </div>
  {/if}
</div>

<style>
  .intake-flow {
    max-width: 720px;
    margin: 0 auto;
  }

  .intake-title {
    font-size: 1.125rem;
    font-weight: 600;
    color: var(--text-primary);
    margin: 0 0 20px 0;
    text-align: center;
  }

  .category-grid {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 12px;
  }

  .category-card {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 8px;
    padding: 24px 16px;
    background: var(--surface-bg);
    border: 1px solid var(--border-default);
    border-radius: 12px;
    cursor: pointer;
    color: var(--text-primary);
    font-family: inherit;
    transition: border-color 200ms, background 200ms;
    text-align: center;
  }

  .category-card:hover {
    border-color: var(--primary);
    background: rgba(78, 154, 249, 0.04);
  }

  .category-card i {
    font-size: 1.5rem;
    color: var(--primary);
  }

  .category-label {
    font-size: 0.9375rem;
    font-weight: 600;
  }

  .category-desc {
    font-size: 0.75rem;
    color: var(--text-muted);
    line-height: 1.3;
  }

  .config-form {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  .config-header {
    display: flex;
    align-items: center;
    gap: 12px;
  }

  .config-header h3 {
    margin: 0;
    font-size: 1rem;
    font-weight: 600;
    color: var(--text-primary);
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .config-header h3 i {
    color: var(--primary);
  }

  .form-group {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .form-group label {
    font-size: 0.8125rem;
    font-weight: 500;
    color: var(--text-secondary);
  }

  .form-row {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 16px;
  }

  .pill-selector {
    display: flex;
    gap: 4px;
  }

  .pill {
    min-height: 44px;
    padding: 6px 14px;
    border: 1px solid var(--border-default);
    border-radius: 20px;
    background: transparent;
    color: var(--text-secondary);
    font-size: 0.8125rem;
    font-family: inherit;
    cursor: pointer;
    transition: all 150ms;
  }

  .pill.active {
    background: var(--primary);
    border-color: var(--primary);
    color: white;
  }

  .pill:hover:not(.active) {
    border-color: var(--text-muted);
  }

  .submit-btn {
    margin-top: 8px;
    align-self: flex-start;
  }

  @media (max-width: 600px) {
    .category-grid { grid-template-columns: repeat(2, 1fr); }
    .form-row { grid-template-columns: 1fr; }
  }
</style>
