<script>
  let { character, branchKind = 'canon' } = $props();
  let traits = $derived(Array.isArray(character?.canonicalTraits ?? character?.canonical_traits) ? (character.canonicalTraits ?? character.canonical_traits) : []);
  let questions = $derived(Array.isArray(character?.openQuestions ?? character?.open_questions) ? (character.openQuestions ?? character.open_questions) : []);
</script>

<article class="character-card" data-testid="creative-character-card">
  <header>
    <div>
      <h3>{character?.displayName ?? character?.display_name ?? character?.id ?? 'Unnamed character'}</h3>
      <p>{character?.summary ?? 'No summary supplied.'}</p>
    </div>
    <span class:exploratory={branchKind === 'exploratory'}>{branchKind}</span>
  </header>

  <dl>
    <div>
      <dt>Traits</dt>
      <dd>{traits.length ? traits.join(', ') : 'No traits supplied'}</dd>
    </div>
    <div>
      <dt>Open Questions</dt>
      <dd>{questions.length ? questions.join(', ') : 'No open questions supplied'}</dd>
    </div>
    <div>
      <dt>Portrait</dt>
      <dd>{character?.portraitRef ?? character?.portrait_ref ?? 'No portrait supplied'}</dd>
    </div>
  </dl>
</article>

<style>
  .character-card {
    display: grid;
    gap: 12px;
    border: 1px solid var(--border-color, #d1d5db);
    border-radius: 8px;
    padding: 14px;
    background: var(--surface-primary, #fff);
    min-width: 0;
  }

  header {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    align-items: flex-start;
  }

  h3,
  p,
  dl,
  dd {
    margin: 0;
  }

  h3 {
    font-size: 1rem;
  }

  p,
  dd {
    color: var(--text-secondary, #64748b);
    line-height: 1.35;
  }

  span {
    border: 1px solid #2563eb;
    border-radius: 999px;
    padding: 4px 8px;
    color: #1d4ed8;
    background: #eff6ff;
    font-size: 0.76rem;
    font-weight: 700;
    text-transform: capitalize;
    white-space: nowrap;
  }

  span.exploratory {
    border-color: #b45309;
    color: #92400e;
    background: #fffbeb;
  }

  dl {
    display: grid;
    gap: 8px;
  }

  div {
    display: grid;
    gap: 3px;
  }

  dt {
    color: var(--text-primary, #111827);
    font-size: 0.78rem;
    font-weight: 700;
  }
</style>
