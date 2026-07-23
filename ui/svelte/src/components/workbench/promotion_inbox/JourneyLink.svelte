<script>
  import { appState } from '$lib/stores/app.svelte.js';
  import {
    buildWorkbenchJourneyHref,
    viewForWorkbenchJourneyTarget,
  } from '$lib/workbench/journey_router.ts';

  let {
    target = 'console',
    projectId = 'default',
    params = {},
    label = 'Open',
    ariaLabel = '',
    disabled = false,
  } = $props();

  let href = $derived(buildWorkbenchJourneyHref(target, { ...params, projectId }));

  function activate(event) {
    if (disabled) {
      event.preventDefault();
      return;
    }
    event.preventDefault();
    window.history.pushState(null, '', href);
    appState.currentProjectId = projectId || 'default';
    appState.currentView = viewForWorkbenchJourneyTarget(target);
  }
</script>

<a
  class:disabled
  class="journey-link"
  href={disabled ? undefined : href}
  aria-disabled={disabled ? 'true' : undefined}
  aria-label={ariaLabel || label}
  tabindex={disabled ? -1 : undefined}
  onclick={activate}
>
  {label}
</a>

<style>
  .journey-link {
    align-items: center;
    border: 1px solid #176d6b;
    border-radius: 6px;
    color: #0f5c59;
    display: inline-flex;
    font-size: 0.82rem;
    font-weight: 700;
    justify-content: center;
    line-height: 1.2;
    min-height: 2rem;
    padding: 0.45rem 0.65rem;
    text-decoration: none;
    white-space: nowrap;
  }

  .journey-link:focus-visible {
    outline: 2px solid #176d6b;
    outline-offset: 2px;
  }

  .journey-link.disabled {
    border-color: #aab3bd;
    color: #68707c;
    cursor: not-allowed;
    opacity: 0.65;
  }
</style>
