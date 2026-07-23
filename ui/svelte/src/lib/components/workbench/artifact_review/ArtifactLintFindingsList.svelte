<script>
  import { asArray } from '$lib/utils/safe.js';
  import { requireEvidence } from '$lib/evidence/evidenceGuard.js';

  let { findings = [], reviewState = 'PENDING' } = $props();

  function evidenceIssue(finding) {
    const refs = asArray(finding.evidence_refs);
    if (refs.length === 0 && finding.severity === 'BLOCKER') {
      return 'missing_blocker_evidence';
    }
    try {
      requireEvidence(refs, `artifact-lint:${finding.rule_id ?? 'finding'}`);
      return '';
    } catch (error) {
      return error.message;
    }
  }

  let normalizedFindings = $derived(findings.map((finding) => ({ ...finding, evidenceIssue: evidenceIssue(finding) })));
  const hasBlocker = $derived(
    normalizedFindings.some((finding) => finding.severity === 'BLOCKER' || finding.evidenceIssue),
  );
  const severityClass = (severity) => `severity-${String(severity).toLowerCase()}`;
</script>

<section class="lint-findings" aria-label="Artifact lint findings">
  {#if reviewState === 'BLOCKED_BY_LINT' || hasBlocker}
    <div data-testid="blocked-by-lint-banner" class="blocked">
      BLOCKED_BY_LINT
    </div>
  {/if}

  <ul>
    {#if findings.length === 0}
      <li class="empty">No lint findings.</li>
    {:else}
      {#each normalizedFindings as finding}
        <li class="finding">
          <div class="finding-header">
            <strong>{finding.rule_id}</strong>
            <span data-testid={severityClass(finding.severity)} class={severityClass(finding.severity)}>
              {finding.severity}
            </span>
          </div>
          <p>{finding.message}</p>
          <p class="location">{finding.location}</p>
          <div class="tags">
            {#each asArray(finding.risk_tags) as tag}
              <span>{tag}</span>
            {/each}
            {#if finding.evidenceIssue}
              <span class="evidence-issue">{finding.evidenceIssue}</span>
            {/if}
          </div>
        </li>
      {/each}
    {/if}
  </ul>
</section>

<style>
  .lint-findings {
    display: grid;
    gap: 12px;
  }

  .blocked {
    padding: 10px 12px;
    border: 1px solid #b91c1c;
    border-radius: 6px;
    background: #fef2f2;
    color: #7f1d1d;
    font-weight: 700;
  }

  ul {
    display: grid;
    gap: 10px;
    padding: 0;
    margin: 0;
    list-style: none;
  }

  .finding,
  .empty {
    border: 1px solid #d1d5db;
    border-radius: 6px;
    padding: 10px;
  }

  .finding-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }

  .severity-info,
  .severity-warning,
  .severity-error,
  .severity-blocker {
    border-radius: 999px;
    padding: 3px 8px;
    font-size: 0.74rem;
    font-weight: 700;
  }

  .severity-info {
    background: #eef2ff;
    color: #3730a3;
  }

  .severity-warning {
    background: #fff7ed;
    color: #9a3412;
  }

  .severity-error,
  .severity-blocker {
    background: #fef2f2;
    color: #991b1b;
  }

  p {
    margin: 8px 0 0;
  }

  .location {
    color: #4b5563;
    font-size: 0.82rem;
  }

  .tags {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-top: 8px;
  }

  .tags span {
    border: 1px solid #d1d5db;
    border-radius: 999px;
    padding: 2px 7px;
    font-size: 0.72rem;
  }

  .tags .evidence-issue {
    border-color: #b91c1c;
    color: #7f1d1d;
  }
</style>
