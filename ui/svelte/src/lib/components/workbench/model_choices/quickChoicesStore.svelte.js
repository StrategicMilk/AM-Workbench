import { workbenchKernelRequest } from '$lib/api.js';
import { requireEvidence } from '$lib/evidence/evidenceGuard.js';

export class QuickChoicesStore {
  catalog = $state(null);
  loading = $state(false);
  error = $state(null);
  repinDecision = $state(null);

  get choicesByQualifiedId() {
    return new Map((this.catalog?.choices ?? []).map((choice) => [choice.model_ref.qualified_id, choice]));
  }

  async loadCatalog(surface) {
    this.loading = true;
    this.error = null;
    try {
      this.catalog = await workbenchKernelRequest(`/api/workbench/model-choices/${encodeURIComponent(surface)}`);
    } catch (err) {
      this.error = err?.message ?? String(err);
      throw err;
    } finally {
      this.loading = false;
    }
  }

  async repin(surface, qualifiedId) {
    this.error = null;
    const choice = this.choicesByQualifiedId.get(qualifiedId);
    try {
      if (!choice?.pinned_version_id) {
        throw new Error('Model pin requires a catalog model id.');
      }
      const refs = [
        ...(Array.isArray(choice?.evidence_refs) ? choice.evidence_refs : []),
        ...(Array.isArray(choice?.provenance_refs) ? choice.provenance_refs : []),
        choice?.policy_ref,
        choice.pinned_version_id,
      ].filter(Boolean);
      if (refs.length === 0) {
        throw new Error('missing_model_choice_evidence_refs');
      }
      requireEvidence(refs, `model-choice:repin:${qualifiedId}`);
      this.repinDecision = await workbenchKernelRequest(`/api/workbench/model-choices/${encodeURIComponent(surface)}/repin`, {
        method: 'POST',
        body: JSON.stringify({
          qualified_id: qualifiedId,
          pinned_version_id: choice.pinned_version_id,
          approval_context: `model-choice:repin:${qualifiedId}`,
        }),
      });
      await this.loadCatalog(surface);
      return this.repinDecision;
    } catch (err) {
      this.error = err?.message ?? String(err);
      throw err;
    }
  }
}
