<script lang="ts">
  import { createEventDispatcher } from 'svelte';

  export let skills: Array<{ name: string; score: number; category: string }> = [];

  const dispatch = createEventDispatcher<{ skillSelected: { skill: { name: string; score: number; category: string } } }>();

  function scoreClass(score: number): string {
    if (score <= 33) return 'low';
    if (score <= 66) return 'medium';
    return 'high';
  }

  function selectSkill(skill: { name: string; score: number; category: string }) {
    dispatch('skillSelected', { skill });
  }
</script>

<div class="skill-heatmap" role="list" aria-label="Skill heatmap">
  {#each skills as skill}
    <button
      class={`skill-cell ${scoreClass(skill.score)}`}
      type="button"
      role="listitem"
      aria-label={`${skill.name}: ${skill.score}`}
      on:click={() => selectSkill(skill)}
    >
      <span class="skill-name">{skill.name}</span>
      <span class="skill-category">{skill.category}</span>
      <span class="skill-score">{skill.score}</span>
    </button>
  {/each}
</div>

<style>
  .skill-heatmap {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(8rem, 1fr));
    gap: 0.5rem;
  }

  .skill-cell {
    display: grid;
    gap: 0.25rem;
    min-height: 5rem;
    padding: 0.75rem;
    border: 1px solid var(--border-color, #d9dde5);
    border-radius: 6px;
    text-align: left;
    color: var(--text-primary, #162033);
    background: var(--surface, #ffffff);
    cursor: pointer;
  }

  .skill-cell.low {
    border-color: #c44b4b;
    background: #fff1f1;
  }

  .skill-cell.medium {
    border-color: #b7791f;
    background: #fff7df;
  }

  .skill-cell.high {
    border-color: #2f855a;
    background: #edf8f1;
  }

  .skill-name {
    font-weight: 650;
  }

  .skill-category {
    font-size: 0.78rem;
    color: var(--text-muted, #526071);
  }

  .skill-score {
    font-variant-numeric: tabular-nums;
    font-weight: 700;
  }
</style>
