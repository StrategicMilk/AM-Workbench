<script>
  import ArtifactReviewPanel from '$lib/components/workbench/artifact_review/ArtifactReviewPanel.svelte';
  import HelpPopover from '$lib/components/help/HelpPopover.svelte';
  import HelpTooltip from '$lib/components/help/HelpTooltip.svelte';

  let { projectId = 'default' } = $props();

  const reviewRouteHref = $derived(`/projects/${encodeURIComponent(projectId || 'default')}/artifact-review`);
</script>

<section class="artifact-review-view" aria-label="Artifact Review workspace">
  <header class="view-header">
    <div>
      <h1>Artifact Review</h1>
      <p>Diff, lint, and review generated artifacts before promotion.</p>
      <HelpPopover
        title="Artifact review"
        body="Reviewable diff gate: an artifact cannot be promoted until at least one reviewer has opened the diff and recorded a review action. Lint failure blocking: if the artifact fails its lint check, promotion is blocked until the lint issues are resolved or explicitly waived with a justification. Raw artifact access: the raw artifact (before diff rendering) is accessible via the overflow menu — use this when you need to copy the full content or compare against an external reference. Reviews are per-artifact-version; a new version resets the review state."
        severity="info"
      />
    </div>
    <a href={reviewRouteHref}>Copy route</a>
  </header>

  <ArtifactReviewPanel {projectId} />
</section>

<style>
  .artifact-review-view {
    display: grid;
    gap: 18px;
    padding: 24px;
  }

  .view-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 16px;
    border-bottom: 1px solid var(--border-default, #d1d5db);
    padding-bottom: 16px;
  }

  h1 {
    margin: 0;
    font-size: 1.45rem;
    font-weight: 700;
  }

  p {
    margin: 6px 0 0;
    color: var(--text-muted, #4b5563);
  }

  a {
    border: 1px solid var(--border-default, #d1d5db);
    border-radius: 6px;
    padding: 8px 10px;
    color: var(--text-primary, #111827);
    font-size: 0.85rem;
    font-weight: 700;
    text-decoration: none;
    white-space: nowrap;
  }

  @media (max-width: 700px) {
    .artifact-review-view {
      padding: 16px;
    }

    .view-header {
      align-items: stretch;
      flex-direction: column;
    }

    a {
      align-self: flex-start;
      white-space: normal;
    }
  }
</style>
