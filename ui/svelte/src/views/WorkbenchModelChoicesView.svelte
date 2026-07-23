<script>
  import { QuickChoicesPanel } from '$lib/components/workbench/model_choices';
  import HelpPopover from '$lib/components/help/HelpPopover.svelte';
  import HelpTooltip from '$lib/components/help/HelpTooltip.svelte';
</script>

<main class="model-choices-view">
  <header class="choices-header">
    <div>
      <h1>Model Choices</h1>
      <p>Review and configure model routing preferences for each workbench surface.</p>
      <HelpPopover
        title="Model choices"
        body="Model references use provider-qualified IDs (e.g. anthropic:claude-sonnet-4-6) to unambiguously identify the model and provider. Inactive reason: a model may be listed as inactive because it is deprecated, over quota, or unavailable in your provider plan — hover the inactive badge for the reason. Surface-specific routing: each workbench surface (mission control, run kernel, annotation, etc.) can be assigned a different model; changes here override the project default for that surface only."
        severity="info"
      />
    </div>
  </header>
  <QuickChoicesPanel />
</main>

<style>
  .model-choices-view {
    width: 100%;
    max-width: 1280px;
    padding: 18px;
  }
  .choices-header { margin-bottom: 16px; }
  .choices-header h1 { margin: 0 0 4px; font-size: 24px; color: var(--text-primary); }
  .choices-header p { margin: 0; color: var(--text-muted); }

  @media (max-width: 760px) {
    .model-choices-view {
      max-width: none;
      overflow-x: hidden;
      padding: 14px;
    }

    .choices-header h1 {
      font-size: 20px;
    }

    .choices-header p {
      overflow-wrap: anywhere;
    }
  }
</style>
