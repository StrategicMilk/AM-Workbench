<script>
  let { projectId = 'default', panel = null } = $props();

  const SECRET_PATTERN = /\b(api[_-]?key|token|secret|password)\b\s*[:=]\s*["']?[^"',\s]+/gi;
  const WINDOWS_PATH_PATTERN = /[A-Za-z]:\\[^\s,;]+/g;

  function redactDisplayText(value) {
    return String(value ?? '')
      .replace(SECRET_PATTERN, '$1=[redacted]')
      .replace(WINDOWS_PATH_PATTERN, '[redacted-path]');
  }

  function redactList(values) {
    return values.map((value) => redactDisplayText(value));
  }

  const evidenceRefs = $derived(panel?.evidenceRefs ?? panel?.evidence_refs ?? []);
  const authorityRefs = $derived(panel?.authorityRefs ?? panel?.authority_refs ?? []);
  const policyGates = $derived(panel?.policyGates ?? panel?.policy_gates ?? []);
  const preferenceEffects = $derived(panel?.preferenceEffects ?? panel?.preference_effects ?? []);
  const nextActions = $derived(panel?.nextActions ?? panel?.next_actions ?? []);
  const blockers = $derived(panel?.blockers ?? []);
  const missing = $derived(panel?.missing ?? []);
  const trusted = $derived(
    !!panel
      && typeof panel.confidence === 'number'
      && evidenceRefs.length > 0
      && authorityRefs.length > 0
      && blockers.length === 0
      && missing.length === 0
  );
</script>

<section class="why-panels-view" aria-labelledby="why-panels-heading">
  <header class="why-panels-header">
    <div>
      <p class="eyebrow">Project {redactDisplayText(projectId)}</p>
      <h1 id="why-panels-heading">Why Panels</h1>
    </div>
    <span class="status-pill">{trusted ? (panel.status ?? 'ready') : 'blocked'}</span>
  </header>

  <div class="why-grid">
    {#if trusted}
      <article class="why-panel" aria-label="Structured decision explanation">
        <div class="panel-heading">
          <div>
            <h2>{redactDisplayText(panel.subject)}</h2>
            <p>{redactDisplayText(panel.chosenOption ?? panel.chosen_option)}</p>
          </div>
          <span>{Math.round(panel.confidence * 100)}% confidence</span>
        </div>

        <dl class="why-metadata">
          <div>
            <dt>Evidence</dt>
            <dd>{redactDisplayText(evidenceRefs.join(', '))}</dd>
          </div>
          <div>
            <dt>Authority</dt>
            <dd>{redactDisplayText(authorityRefs.join(', '))}</dd>
          </div>
          <div>
            <dt>Provenance</dt>
            <dd>verified</dd>
          </div>
        </dl>

        <div class="section-row">
          <h3>Policy gates</h3>
          <ul>
            {#each redactList(policyGates) as gate}
              <li>{gate}</li>
            {/each}
          </ul>
        </div>

        <div class="section-row">
          <h3>User preference effects</h3>
          <ul>
            {#each redactList(preferenceEffects) as effect}
              <li>{effect}</li>
            {/each}
          </ul>
        </div>

        <div class="section-row">
          <h3>Next actions</h3>
          <ul>
            {#each redactList(nextActions) as action}
              <li>{action}</li>
            {/each}
          </ul>
        </div>
      </article>
    {:else}
      <article class="why-panel blocked" aria-label="Structured decision explanation unavailable">
        <div class="panel-heading">
          <div>
            <h2>Trusted explanation unavailable</h2>
            <p>Runtime evidence, authority, provenance, and confidence are required.</p>
          </div>
          <span>Confidence missing</span>
        </div>
        <dl class="why-metadata">
          <div>
            <dt>Evidence</dt>
            <dd>{evidenceRefs.length ? redactDisplayText(evidenceRefs.join(', ')) : 'missing'}</dd>
          </div>
          <div>
            <dt>Authority</dt>
            <dd>{authorityRefs.length ? redactDisplayText(authorityRefs.join(', ')) : 'missing'}</dd>
          </div>
          <div>
            <dt>Provenance</dt>
            <dd>{redactDisplayText([...missing, ...blockers].join(', ') || 'missing runtime why panel payload')}</dd>
          </div>
        </dl>
        <div class="section-row">
          <h3>Next actions</h3>
          <ul>
            <li>Attach a trusted why panel payload</li>
          </ul>
        </div>
      </article>
    {/if}
  </div>
</section>

<style>
  .why-panels-view {
    display: flex;
    flex-direction: column;
    gap: 20px;
    padding: 24px;
    color: var(--text-primary);
  }

  .why-panels-header,
  .panel-heading {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 16px;
  }

  .eyebrow {
    margin: 0 0 4px;
    color: var(--text-muted);
    font-size: 12px;
    text-transform: uppercase;
  }

  h1,
  h2,
  h3,
  p {
    margin: 0;
  }

  .status-pill,
  .panel-heading span {
    border: 1px solid var(--border-default);
    border-radius: 6px;
    padding: 4px 8px;
    color: var(--warning, #f59e0b);
    font-size: 12px;
    text-transform: uppercase;
    white-space: nowrap;
  }

  .why-grid {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    gap: 16px;
  }

  .why-panel {
    border: 1px solid var(--border-default);
    border-radius: 8px;
    padding: 20px;
    background: var(--surface-elevated, #1a202d);
  }

  .why-metadata {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 12px;
    margin: 20px 0;
  }

  dt {
    color: var(--text-muted);
    font-size: 12px;
    text-transform: uppercase;
  }

  dd {
    margin: 4px 0 0;
  }

  .section-row {
    border-top: 1px solid var(--border-default);
    padding-top: 14px;
    margin-top: 14px;
  }

  ul {
    margin: 8px 0 0;
    padding-left: 18px;
  }
</style>
