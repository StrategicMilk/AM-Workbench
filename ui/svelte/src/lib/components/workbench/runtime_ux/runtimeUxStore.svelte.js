import { CardStatus } from '$lib/contracts';
import { requireEvidence } from '$lib/evidence/evidenceGuard.js';

const DEFAULT_RUNTIME_UX = {
  run: {
    run_id: null,
    stream_id: null,
    status: 'unavailable',
    step_state: 'unknown',
    replay_status: CardStatus.UNAVAILABLE
  },
  events: [],
  cards: []
};

export function createRuntimeUxState(initial = {}) {
  let state = $state({
    ...DEFAULT_RUNTIME_UX,
    ...initial,
    run: { ...DEFAULT_RUNTIME_UX.run, ...(initial.run ?? {}) },
    cards: Array.isArray(initial.cards) ? initial.cards : [],
    events: Array.isArray(initial.events) ? initial.events : []
  });
  let summary = $derived({
    blocked: state.cards.filter((card) => [CardStatus.BLOCKED, CardStatus.UNAVAILABLE, CardStatus.REPLAY_MISMATCH].includes(card.status)).length,
    degraded: state.cards.filter((card) => [CardStatus.APPROVAL_REQUIRED, CardStatus.DEGRADED, CardStatus.STALE].includes(card.status)).length,
    allowed: state.cards.filter((card) => card.status === CardStatus.ALLOWED).length
  });

  function validateRuntimeEvidence(cards) {
    const refs = cards.flatMap((card) => card.evidence_refs ?? card.evidence ?? []);
    if (refs.length > 0) {
      requireEvidence(refs, 'runtime_ux.cards.evidence_refs');
    }
  }

  return {
    get state() {
      return state;
    },
    get summary() {
      return summary;
    },
    setRun(next) {
      state.run = { ...state.run, ...next };
    },
    setCards(cards) {
      const nextCards = Array.isArray(cards) ? cards : [];
      validateRuntimeEvidence(nextCards);
      state.cards = nextCards;
    },
    async loadSnapshot(loadRuntimeUx, projectId = 'default') {
      const snapshot = await loadRuntimeUx(projectId);
      if (!snapshot || typeof snapshot !== 'object') {
        throw new Error('runtime_ux_snapshot_unavailable');
      }
      state.run = { ...state.run, ...(snapshot.run ?? {}) };
      if (Array.isArray(snapshot.cards)) {
        validateRuntimeEvidence(snapshot.cards);
      }
      state.cards = Array.isArray(snapshot.cards) ? snapshot.cards : state.cards;
      state.events = Array.isArray(snapshot.events) ? snapshot.events : state.events;
      return state;
    }
  };
}
