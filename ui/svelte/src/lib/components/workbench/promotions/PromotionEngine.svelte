<script>
  import * as api from '$lib/api.js';

  let { projectId = 'default', onPromoteRecipe = null } = $props();

  const fallbackRecipes = [
    { id: 'conversation_excerpt', label: 'Conversation Excerpt', degraded: true },
    { id: 'evidence_asset', label: 'Evidence Asset', degraded: true },
    { id: 'evidence_notebook', label: 'Evidence Notebook', degraded: true },
    { id: 'preference_card', label: 'Preference Card', degraded: true },
    { id: 'eval_case', label: 'Eval Case', degraded: true },
    { id: 'automation_recipe', label: 'Automation Recipe', degraded: true },
    { id: 'data_asset', label: 'Data Asset', degraded: true },
  ];
  let recipes = $state([]);
  let recipeState = $state('loading');

  $effect(() => {
    let cancelled = false;
    api.getPromotionRecipes(projectId)
      .then((result) => {
        if (cancelled) return;
        const rows = Array.isArray(result?.recipes) ? result.recipes : [];
        recipes = rows.length
          ? rows.map((recipe) => ({
              id: String(recipe.id ?? recipe.recipe_id ?? recipe.label),
              label: String(recipe.label ?? recipe.name ?? recipe.id),
              degraded: false,
            }))
          : fallbackRecipes;
        recipeState = rows.length ? 'api' : 'blocked';
      })
      .catch(() => {
        if (!cancelled) {
          recipes = fallbackRecipes;
          recipeState = 'blocked';
        }
      });
    return () => {
      cancelled = true;
    };
  });
</script>

<section class="promotion-engine" aria-labelledby="promotion-title" data-project-id={projectId} data-recipe-state={recipeState}>
  <header>
    <h1 id="promotion-title">Promotion Engine</h1>
    <p>Turn useful conversation ranges into reversible structured artifacts when a contextual action is worth taking.</p>
  </header>

  <div class="recipe-grid">
    {#each recipes as recipe}
      <article data-degraded={recipe.degraded}>
        <h2>{recipe.label}</h2>
        <p>Requires source range, provenance, and a typed target recipe.</p>
        <button type="button" disabled={recipe.degraded} onclick={() => onPromoteRecipe?.({ project_id: projectId, recipe_id: recipe.id })}>
          Promote
        </button>
      </article>
    {/each}
  </div>
</section>

<style>
  .promotion-engine {
    display: grid;
    gap: 20px;
    padding: 24px;
  }

  h1 {
    margin: 0 0 8px;
    font-size: 28px;
  }

  p {
    margin: 0;
    color: var(--text-secondary, #4b5563);
  }

  .recipe-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
    gap: 12px;
  }

  article {
    min-height: 112px;
    padding: 14px;
    border: 1px solid var(--border-color, #d1d5db);
    border-radius: 8px;
    background: var(--surface-primary, #fff);
  }

  article[data-degraded='true'] {
    border-color: var(--warning, #b45309);
  }

  h2 {
    margin: 0 0 8px;
    font-size: 17px;
  }
</style>
