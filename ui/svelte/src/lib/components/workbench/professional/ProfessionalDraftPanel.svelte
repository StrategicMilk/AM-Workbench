<script>
  import SensitiveWorkflowDecisionCard from '../life_admin/SensitiveWorkflowDecisionCard.svelte';
  import SensitiveWorkflowPanel from '../life_admin/SensitiveWorkflowPanel.svelte';
  import PromotedArtifactCard from './PromotedArtifactCard.svelte';
  import { workbenchKernelRequest } from '$lib/api.js';
  import { requireEvidence } from '$lib/evidence/evidenceGuard.js';

  let { projectId = 'default' } = $props();

  let lastDecision = $state(null);
  let draftText = $state('');
  let promotedRecord = $state(null);
  let rejectionReasons = $state([]);

  let canPromote = $derived(Boolean(lastDecision && lastDecision.allowed && !lastDecision.degraded && draftText.trim().length > 0));

  function handleDecision(decision) {
    lastDecision = decision;
    promotedRecord = null;
    rejectionReasons = [];
  }

  async function promoteArtifact() {
    if (!lastDecision || !lastDecision.allowed || lastDecision.degraded || draftText.trim().length === 0) {
      rejectionReasons = ['promotion_requires_allowed_decision_and_draft'];
      return;
    }
    const sourceCardIds = requireEvidence(lastDecision.evidence_refs ?? [], 'professional_draft.source_card_ids');
    if (sourceCardIds.length === 0) {
      rejectionReasons = ['promotion_requires_evidence_refs'];
      return;
    }
    const payload = {
      draft_text: draftText.trim(),
      artifact_kind: lastDecision.promoted_artifact_kind ?? 'professional_memo',
      project_id: projectId,
      provenance: [
        ['policy_explanation_ref', lastDecision.policy_explanation_ref],
        ['rigor_required', lastDecision.rigor_required],
        ['mode_lens_id', lastDecision.mode_lens_id],
        ['decision_id', lastDecision.decision_id],
      ],
      source_card_ids: sourceCardIds,
      tool_card_ids: [],
      claim_promotion_decision_ref: lastDecision.policy_explanation_ref,
      mode_lens_id: lastDecision.mode_lens_id,
      rigor_level: lastDecision.rigor_required,
    };
    try {
      const result = await workbenchKernelRequest(`/api/workbench/projects/${encodeURIComponent(projectId)}/promoted-artifacts`, {
        method: 'POST',
        body: JSON.stringify(payload),
      });
      if (result?.error) throw new Error(result.error);
      promotedRecord = result.artifact ?? result.promoted_artifact;
      if (!promotedRecord) throw new Error('promotion_api_missing_artifact_record');
      rejectionReasons = [];
    } catch (error) {
      promotedRecord = null;
      rejectionReasons = [error instanceof Error ? error.message : 'promotion_api_unavailable'];
    }
  }
</script>

<section class="professional-draft-panel" aria-label="Professional draft panel">
  <SensitiveWorkflowPanel projectId={projectId} onDecision={handleDecision} />

  <div class="draft-layout">
    <div class="draft-editor">
      <h2>Draft</h2>
      <textarea bind:value={draftText} rows="8" aria-label="Professional draft"></textarea>
      <button type="button" onclick={promoteArtifact} disabled={!canPromote}>Promote artifact</button>
      {#if rejectionReasons.length}
        <ul class="rejections" aria-label="Promotion rejection reasons">
          {#each rejectionReasons as reason}
            <li>{reason}</li>
          {/each}
        </ul>
      {/if}
    </div>

    <div class="preview-stack">
      <SensitiveWorkflowDecisionCard decision={lastDecision} />
      <PromotedArtifactCard record={promotedRecord} />
    </div>
  </div>
</section>

<style>
  .professional-draft-panel {
    display: grid;
    gap: 16px;
    color: var(--text-primary, #111827);
  }

  .draft-layout {
    display: grid;
    grid-template-columns: minmax(280px, 0.9fr) minmax(320px, 1.1fr);
    gap: 14px;
    padding: 0 18px 18px;
  }

  .draft-editor,
  .preview-stack {
    display: grid;
    gap: 10px;
    align-content: start;
  }

  h2 {
    margin: 0;
    font-size: 1.05rem;
  }

  textarea {
    width: 100%;
    min-height: 180px;
    resize: vertical;
    border: 1px solid var(--border-color, #d1d5db);
    border-radius: 8px;
    background: var(--surface-primary, #fff);
    color: inherit;
    padding: 10px;
    font: inherit;
  }

  button {
    width: fit-content;
    min-height: 34px;
    border: 1px solid var(--accent-color, #2563eb);
    border-radius: 6px;
    background: var(--surface-primary, #fff);
    color: inherit;
    padding: 4px 10px;
    cursor: pointer;
  }

  button:disabled {
    cursor: not-allowed;
    opacity: 0.55;
  }

  .rejections {
    margin: 0;
    color: #d9480f;
  }

  @media (max-width: 900px) {
    .draft-layout {
      grid-template-columns: 1fr;
    }
  }
</style>
