<script>
  import BranchSwitcher from './BranchSwitcher.svelte';
  import CharacterCardView from './CharacterCardView.svelte';
  import ContinuityCheckPanel from './ContinuityCheckPanel.svelte';
  import WorldBibleView from './WorldBibleView.svelte';
  import * as api from '$lib/api.js';
  import { BranchKind, CreativeViolationKind, ReadinessState } from '$lib/contracts';

  let { projectId = 'default' } = $props();

  let studioState = $state('loading');
  let studioError = $state('');
  let world = $state({
    worldId: 'world-arcadia',
    title: 'Arcadia Gate',
    summary: 'A border city built around a sealed gate that remembers every route through it.',
    authority_ref: 'authority:creative-world:arcadia',
    provenance_ref: 'provenance:creative-world:arcadia',
    evidence_refs: ['evidence:creative-world:arcadia'],
  });
  let toneGuide = $state({
    voiceSummary: 'Luminous mystery',
    styleRules: ['Concrete sensory anchors', 'Wonder with consequence', 'No detached irony'],
    authority_ref: 'authority:tone-guide:arcadia',
    provenance_ref: 'provenance:tone-guide:arcadia',
  });
  let timeline = $state([
    { eventId: 'arrival', sequence: 1, summary: 'Mira reaches the sealed gate.', evidence_refs: ['evidence:scene:arrival'] },
    { eventId: 'echo-market', sequence: 2, summary: 'The market repeats a conversation from ten years ago.', evidence_refs: ['evidence:scene:echo-market'] },
    { eventId: 'ledger-theft', sequence: 3, summary: 'Vale hides the crossing ledger.', evidence_refs: ['evidence:scene:ledger-theft'] },
  ]);
  let relationships = $state([
    { source: 'Mira', target: 'Vale', type: 'rival', evidence_refs: ['evidence:relationship:mira-vale'] },
    { source: 'Mira', target: 'Gate Choir', type: 'protected by', evidence_refs: ['evidence:relationship:mira-gate-choir'] },
  ]);
  let characters = $state([
    {
      id: 'hero',
      displayName: 'Mira',
      summary: 'A courier who remembers routes nobody else can see.',
      canonicalTraits: ['brave', 'observant'],
      openQuestions: ['Why can Mira see old roads?'],
      portraitRef: 'media:portrait-mira',
      branchId: 'canon-main',
      authority_ref: 'authority:character:mira',
      provenance_ref: 'provenance:character:mira',
    },
    {
      id: 'villain',
      displayName: 'Vale',
      summary: 'A magistrate hiding the gate history.',
      canonicalTraits: ['patient', 'secretive'],
      openQuestions: ['Who taught Vale the ritual?'],
      portraitRef: 'media:portrait-vale',
      branchId: 'explore-vale',
      authority_ref: 'authority:character:vale',
      provenance_ref: 'provenance:character:vale',
    },
  ]);
  let canonBranches = $state([{ id: 'canon-main', label: 'Main Canon', worldId: 'world-arcadia' }]);
  let exploratoryBranches = $state([{ id: 'explore-vale', label: 'Vale Redemption Draft', worldId: 'world-arcadia' }]);
  let rejection = $state({
    type: 'CreativeBranchIsolationRejected',
    blockers: ['promotion-authority-required', 'promotion-evidence-required'],
  });
  let cleanViolations = $state([]);
  let exploratoryViolations = $state([
    {
      kind: CreativeViolationKind.TRAIT_CONTRADICTION,
      subjectRef: 'villain',
      description: 'The branch adds both secretive and not:secretive as canonical traits.',
    },
    {
      kind: CreativeViolationKind.TIMELINE_EVENT_UNKNOWN,
      subjectRef: 'scene-draft-4',
      description: 'The scene references a crossing event outside the declared timeline.',
    },
  ]);

  let activeBranch = $state('canon-main');
  let canonBranchIds = $derived(new Set(canonBranches.map((branch) => branch.id)));
  let activeBranchKind = $derived(canonBranchIds.has(activeBranch) ? BranchKind.CANON : BranchKind.EXPLORATORY);
  let visibleCharacters = $derived(
    characters.filter((character) => activeBranchKind === BranchKind.CANON ? (character.branchId ?? character.branch_id) === activeBranch : true)
  );
  let activeViolations = $derived(activeBranchKind === BranchKind.CANON ? cleanViolations : exploratoryViolations);

  $effect(() => {
    let cancelled = false;
    api.getCreativeRoleplayStudio(projectId)
      .then((result) => {
        if (cancelled) return;
        world = result?.world ?? world;
        toneGuide = result?.toneGuide ?? result?.tone_guide ?? toneGuide;
        timeline = Array.isArray(result?.timeline) ? result.timeline : timeline;
        relationships = Array.isArray(result?.relationships) ? result.relationships : relationships;
        characters = Array.isArray(result?.characters) ? result.characters : characters;
        canonBranches = Array.isArray(result?.canonBranches ?? result?.canon_branches)
          ? (result.canonBranches ?? result.canon_branches)
          : canonBranches;
        exploratoryBranches = Array.isArray(result?.exploratoryBranches ?? result?.exploratory_branches)
          ? (result.exploratoryBranches ?? result.exploratory_branches)
          : exploratoryBranches;
        activeBranch = (canonBranches[0] ?? exploratoryBranches[0])?.id ?? activeBranch;
        rejection = result?.rejection ?? rejection;
        cleanViolations = Array.isArray(result?.cleanViolations ?? result?.clean_violations)
          ? (result.cleanViolations ?? result.clean_violations)
          : [];
        exploratoryViolations = Array.isArray(result?.exploratoryViolations ?? result?.exploratory_violations)
          ? (result.exploratoryViolations ?? result.exploratory_violations)
          : exploratoryViolations;
        studioState = 'api';
        studioError = '';
      })
      .catch((error) => {
        if (!cancelled) {
          studioState = ReadinessState.BLOCKED;
          studioError = `creative_studio_unavailable:${error?.message ?? 'unknown'}`;
        }
      });
    return () => {
      cancelled = true;
    };
  });
</script>

<section class="creative-studio" data-testid="creative-roleplay-studio" data-project-id={projectId} data-studio-state={studioState}>
  <header class="studio-header">
    <div>
      <h1>Creative Roleplay Studio</h1>
      <p>{world.summary}</p>
    </div>
    <div class="lens-chip">
      <span>Mode Lens</span>
      <strong>creative_exploration</strong>
    </div>
  </header>
  {#if studioError}
    <p class="studio-error">{studioError}</p>
  {/if}

  <div class="studio-grid">
    <div class="main-column">
      <WorldBibleView {world} {toneGuide} {timeline} {relationships} />

      <section class="character-grid" aria-label="Character cards">
        {#each visibleCharacters as character}
          <CharacterCardView {character} branchKind={canonBranchIds.has(character.branchId ?? character.branch_id) ? BranchKind.CANON : BranchKind.EXPLORATORY} />
        {/each}
      </section>
    </div>

    <aside class="control-column">
      <BranchSwitcher
        {canonBranches}
        {exploratoryBranches}
        activeBranch={activeBranch}
        {rejection}
        onBranchChange={(branchId) => (activeBranch = branchId)}
      />
      <ContinuityCheckPanel violations={activeViolations} />
      <section class="export-plan" aria-label="Export plan">
        <h3>Export Plan</h3>
        <dl>
          <div>
            <dt>Story</dt>
            <dd>authority:export + evidence:scene-ledger</dd>
          </div>
          <div>
            <dt>Script</dt>
            <dd>authority:export + evidence:dialogue-map</dd>
          </div>
          <div>
            <dt>Media Plan</dt>
            <dd>authority:export + evidence:asset-refs</dd>
          </div>
        </dl>
      </section>
    </aside>
  </div>
</section>

<style>
  .creative-studio {
    display: grid;
    gap: 16px;
    max-width: 1260px;
    padding: 18px;
    color: var(--text-primary, #111827);
  }

  .studio-header {
    display: flex;
    justify-content: space-between;
    gap: 16px;
    align-items: flex-start;
  }

  h1,
  h3,
  p,
  dl,
  dd {
    margin: 0;
  }

  h1 {
    font-size: 1.45rem;
  }

  .studio-header p,
  dd,
  .lens-chip span {
    color: var(--text-secondary, #64748b);
  }

  .lens-chip {
    display: grid;
    gap: 4px;
    min-width: 180px;
    border: 1px solid var(--border-color, #d1d5db);
    border-radius: 8px;
    padding: 10px;
    background: var(--surface-secondary, #f8fafc);
    font-size: 0.84rem;
  }

  .studio-grid {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(320px, 0.38fr);
    gap: 14px;
    align-items: start;
  }

  .main-column,
  .control-column {
    display: grid;
    gap: 14px;
    min-width: 0;
  }

  .character-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 12px;
  }

  .export-plan {
    display: grid;
    gap: 10px;
    border: 1px solid var(--border-color, #d1d5db);
    border-radius: 8px;
    padding: 14px;
    background: var(--surface-secondary, #f8fafc);
  }

  .export-plan dl {
    display: grid;
    gap: 8px;
  }

  .export-plan div {
    display: grid;
    gap: 3px;
  }

  dt {
    font-weight: 700;
  }

  @media (max-width: 980px) {
    .studio-header,
    .studio-grid,
    .character-grid {
      display: grid;
      grid-template-columns: 1fr;
    }
  }
</style>
