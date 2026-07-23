<script>
  import { onMount } from 'svelte';
  import ArtifactDiffView from './ArtifactDiffView.svelte';
  import ArtifactLintFindingsList from './ArtifactLintFindingsList.svelte';
  import { workbenchKernelRequest } from '$lib/api.js';
  import { showToast } from '../../../stores/toast.svelte.js';

  let { projectId = 'default' } = $props();

  let review = $state(null);
  let pendingItems = $state([]);
  let selectedItemId = $state('');
  let loading = $state(false);
  let errorMessage = $state('');
  let beforeArtifact = $state('');
  let afterArtifact = $state('');

  const stateBadge = $derived(String(review?.review_state ?? 'PENDING').toLowerCase().replaceAll('_', '-'));
  const reviewStateLabels = {
    PENDING: 'Pending',
    LINT_RUNNING: 'Lint running',
    LINT_PASSED: 'Lint passed',
    LINT_FAILED: 'Lint failed',
    APPROVED: 'Approved',
    REJECTED: 'Rejected',
    BLOCKED_BY_LINT: 'Blocked by lint'
  };
  const reviewStates = Object.keys(reviewStateLabels);
  const reviewStateLabel = (state) => reviewStateLabels[state] ?? 'Unknown review state';
  const selectedItem = $derived(pendingItems.find((item) => item.item_id === selectedItemId) ?? null);

  function normalizeArtifact(value) {
    if (typeof value === 'string') {
      try {
        return JSON.parse(value);
      } catch {
        return { text: value };
      }
    }
    return value && typeof value === 'object' ? value : {};
  }

  function normalizePendingItem(item, index) {
    const itemId = String(item?.item_id ?? item?.subject_id ?? item?.id ?? `pending-${index}`);
    return {
      item_id: itemId,
      subject_id: String(item?.subject_id ?? itemId),
      kind: String(item?.kind ?? item?.artifact_kind ?? 'evidence_asset'),
      label: String(item?.label ?? item?.title ?? item?.subject_id ?? itemId),
      before_artifact: normalizeArtifact(item?.before_artifact ?? item?.before ?? item?.current_artifact),
      after_artifact: normalizeArtifact(item?.after_artifact ?? item?.after ?? item?.candidate_artifact),
    };
  }

  async function loadPendingItems() {
    loading = true;
    errorMessage = '';
    try {
      const body = await workbenchKernelRequest(`/api/workbench/artifact-reviews/pending?project_id=${encodeURIComponent(projectId)}`);
      const items = Array.isArray(body?.items) ? body.items : Array.isArray(body?.pending_reviews) ? body.pending_reviews : [];
      pendingItems = items.map(normalizePendingItem);
      const firstItem = pendingItems[0] ?? null;
      selectedItemId = firstItem?.item_id ?? '';
      beforeArtifact = firstItem ? JSON.stringify(firstItem.before_artifact, null, 2) : '';
      afterArtifact = firstItem ? JSON.stringify(firstItem.after_artifact, null, 2) : '';
    } catch (error) {
      pendingItems = [];
      selectedItemId = '';
      beforeArtifact = '';
      afterArtifact = '';
      errorMessage = error instanceof Error ? error.message : 'Pending artifact reviews unavailable';
    } finally {
      loading = false;
    }
  }

  function selectPendingItem(itemId) {
    selectedItemId = itemId;
    const item = pendingItems.find((pending) => pending.item_id === itemId);
    beforeArtifact = item ? JSON.stringify(item.before_artifact, null, 2) : '';
    afterArtifact = item ? JSON.stringify(item.after_artifact, null, 2) : '';
  }

  async function startReview() {
    if (!selectedItem) {
      errorMessage = 'No pending artifact review selected.';
      return;
    }
    loading = true;
    errorMessage = '';
    try {
      const body = await workbenchKernelRequest('/api/workbench/artifact-reviews', {
        method: 'POST',
        body: JSON.stringify({
          project_id: projectId,
          subject_id: selectedItem?.subject_id,
          kind: selectedItem?.kind,
          before_artifact: JSON.parse(beforeArtifact),
          after_artifact: JSON.parse(afterArtifact)
        })
      });
      review = body;
      if (body?.status === 'blocked_by_lint' || body?.review_state === 'BLOCKED_BY_LINT') {
        showToast('Artifact review blocked by lint findings.', 'warning');
      }
    } catch (error) {
      errorMessage = error instanceof Error ? error.message : 'Artifact review failed';
      showToast(errorMessage, 'error');
    } finally {
      loading = false;
    }
  }

  onMount(() => {
    void loadPendingItems();
  });
</script>

<section class="artifact-review-panel" aria-label="Artifact review">
  <header>
    <h2>Artifact Review</h2>
    <button type="button" onclick={startReview} disabled={loading || !selectedItem}>
      {loading ? 'Reviewing' : 'Review'}
    </button>
  </header>

  {#if pendingItems.length > 0}
    <label>
      Pending artifact
      <select value={selectedItemId} onchange={(event) => selectPendingItem(event.currentTarget.value)}>
        {#each pendingItems as item}
          <option value={item.item_id}>{item.label}</option>
        {/each}
      </select>
    </label>
  {/if}

  <div class="editor-grid">
    <label>
      Before
      <textarea bind:value={beforeArtifact}></textarea>
    </label>
    <label>
      After
      <textarea bind:value={afterArtifact}></textarea>
    </label>
  </div>

  {#if errorMessage}
    <p class="error">{errorMessage}</p>
  {/if}

  {#if review}
    <div class="state-row">
      {#each reviewStates as state}
        <span
          data-testid={`review-state-${state.toLowerCase().replaceAll('_', '-')}`}
          class:active={stateBadge === state.toLowerCase().replaceAll('_', '-')}
        >
          {reviewStateLabel(state)}
        </span>
      {/each}
    </div>
    <ArtifactLintFindingsList findings={review.lint_findings} reviewState={review.review_state} />
    <ArtifactDiffView {review} />
  {/if}
</section>

<style>
  .artifact-review-panel {
    display: grid;
    gap: 16px;
    padding: 16px;
  }

  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }

  h2 {
    margin: 0;
    font-size: 1.2rem;
  }

  button {
    border: 1px solid #111827;
    border-radius: 6px;
    padding: 8px 12px;
    background: #111827;
    color: #ffffff;
    font-weight: 700;
  }

  button:disabled {
    opacity: 0.65;
  }

  .editor-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
    gap: 12px;
  }

  label {
    display: grid;
    gap: 6px;
    font-size: 0.9rem;
    font-weight: 700;
  }

  textarea {
    min-height: 130px;
    resize: vertical;
    border: 1px solid #d1d5db;
    border-radius: 6px;
    padding: 10px;
    font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  }

  select {
    min-height: 36px;
    border: 1px solid #d1d5db;
    border-radius: 6px;
    padding: 6px 8px;
  }

  .error {
    margin: 0;
    color: #991b1b;
  }

  .state-row {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }

  .state-row span {
    border: 1px solid #d1d5db;
    border-radius: 999px;
    padding: 4px 8px;
    color: #4b5563;
    font-size: 0.72rem;
    font-weight: 700;
  }

  .state-row .active {
    border-color: #0066cc;
    background: #eef6ff;
    color: #003f7f;
  }
</style>
