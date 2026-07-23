"""Debate Protocol — multi-agent deliberation for high-stakes decisions.

When a plan node involves architecture or security decisions with low
confidence, the debate protocol surfaces conflicting perspectives from
multiple agent personas and converges them into a consensus position.

Convergence is declared when the Jaccard similarity of key-point sets
across consecutive rounds exceeds CONVERGENCE_THRESHOLD (0.85).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from vetinari.types import GoalCategory

logger = logging.getLogger(__name__)


CONVERGENCE_THRESHOLD = 0.85  # Jaccard similarity to declare consensus
MAX_DEBATE_ROUNDS = 5  # Hard cap on deliberation rounds

# Task categories eligible for debate (architecture + security decisions)
DEBATE_ELIGIBLE_TYPES: frozenset[GoalCategory] = frozenset({
    GoalCategory.SECURITY,
    GoalCategory.CODE,  # architecture-level code decisions
    GoalCategory.DEVOPS,  # infra/deployment decisions
    GoalCategory.DATA,  # schema decisions
})

# Confidence below this triggers debate eligibility check
LOW_CONFIDENCE_THRESHOLD = 0.6


@dataclass
class DebatePosition:
    """A single agent persona's position in a debate round.

    Args:
        persona: Label for the debating perspective (e.g. ``"security_hawk"``).
        stance: A short position statement.
        key_points: Supporting bullet-point claims.
        confidence: Self-assessed confidence in the stance (0.0-1.0).
        round_number: Which debate round this position was recorded in.
    """

    persona: str
    stance: str
    key_points: list[str] = field(default_factory=list)
    confidence: float = 0.5
    round_number: int = 1

    def __repr__(self) -> str:
        return (
            f"DebatePosition(persona={self.persona!r}, confidence={self.confidence:.2f}, round={self.round_number!r})"
        )


@dataclass
class DebateResult:
    """Outcome of a completed debate protocol run.

    Args:
        converged: True if consensus was reached within MAX_DEBATE_ROUNDS.
        consensus_points: Key points agreed upon by all personas.
        dissenting_points: Points that remained contested at close.
        rounds_taken: Number of debate rounds executed.
        final_positions: The last position from each persona.
        recommendation: Synthesized recommendation from the moderator.
    """

    converged: bool
    consensus_points: list[str] = field(default_factory=list)
    dissenting_points: list[str] = field(default_factory=list)
    rounds_taken: int = 0
    final_positions: list[DebatePosition] = field(default_factory=list)
    recommendation: str = ""

    def __repr__(self) -> str:
        return (
            f"DebateResult(converged={self.converged!r}, "
            f"rounds={self.rounds_taken!r}, "
            f"consensus_points={len(self.consensus_points)!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dictionary.

        Returns:
            Dictionary with all debate result fields.
        """
        return {
            "converged": self.converged,
            "consensus_points": self.consensus_points,
            "dissenting_points": self.dissenting_points,
            "rounds_taken": self.rounds_taken,
            "recommendation": self.recommendation,
            "final_positions": [
                {
                    "persona": p.persona,
                    "stance": p.stance,
                    "key_points": p.key_points,
                    "confidence": p.confidence,
                    "round_number": p.round_number,
                }
                for p in self.final_positions
            ],
        }


def should_trigger_debate(
    task_category: GoalCategory,
    confidence: float,
    force: bool = False,
) -> bool:
    """Determine whether a task warrants multi-agent debate.

    A debate is triggered when the task falls in DEBATE_ELIGIBLE_TYPES AND
    the estimated confidence is below LOW_CONFIDENCE_THRESHOLD, OR when
    *force* is explicitly set.

    Args:
        task_category: The GoalCategory of the task being evaluated.
        confidence: Estimated confidence in the current plan (0.0-1.0).
        force: If True, trigger debate regardless of category/confidence.

    Returns:
        True if a debate should be initiated.
    """
    if force:
        return True
    return task_category in DEBATE_ELIGIBLE_TYPES and confidence < LOW_CONFIDENCE_THRESHOLD


def _jaccard(set_a: set[str], set_b: set[str]) -> float:
    """Compute Jaccard similarity between two sets of strings.

    Args:
        set_a: First set.
        set_b: Second set.

    Returns:
        Jaccard similarity coefficient in [0.0, 1.0].
    """
    if not set_a and not set_b:
        return 1.0
    union = set_a | set_b
    if not union:
        return 1.0
    return len(set_a & set_b) / len(union)


class DebateProtocol:
    """Moderates a multi-round deliberation between agent personas.

    Convergence is detected by comparing the union of key points across
    all personas between consecutive rounds.  When Jaccard similarity of
    the key-point union reaches CONVERGENCE_THRESHOLD the debate closes.

    Example::

        protocol = DebateProtocol(topic="Which auth strategy?")
        positions_r1 = [
            DebatePosition("security_hawk", "Use mTLS", ["cert rotation", "no passwords"]),
            DebatePosition("pragmatist", "Use JWT", ["simpler", "stateless"]),
        ]
        protocol.add_round(positions_r1)
        result = protocol.finalize()
    """

    def __init__(self, topic: str, max_rounds: int = MAX_DEBATE_ROUNDS) -> None:
        """Create a new debate session.

        Args:
            topic: Description of the decision being debated.
            max_rounds: Hard upper bound on deliberation rounds.
        """
        self._topic = topic
        self._max_rounds = max_rounds
        self._rounds: list[list[DebatePosition]] = []

    def add_round(self, positions: list[DebatePosition]) -> bool:
        """Record one round of debate positions.

        Args:
            positions: The positions taken by each persona this round.

        Returns:
            True if convergence was detected after this round (debate can close).
        """
        if not positions:
            logger.warning("[DebateProtocol] Empty positions list for round %d", len(self._rounds) + 1)
            return False

        self._rounds.append(positions)
        converged = self._check_convergence()
        logger.debug(
            "[DebateProtocol] Round %d added (%d positions), converged=%s",
            len(self._rounds),
            len(positions),
            converged,
        )
        return converged

    @staticmethod
    def _all_key_points(round_positions: list[DebatePosition]) -> set[str]:
        """Flatten all key points from a round into a normalized set.

        Args:
            round_positions: Positions from a single debate round.

        Returns:
            Set of lowercased, stripped key-point strings.
        """
        result: set[str] = set()
        for pos in round_positions:
            for kp in pos.key_points:
                normalized = kp.strip().lower()
                if normalized:
                    result.add(normalized)
        return result

    def _check_convergence(self) -> bool:
        """Check whether the last two rounds have converged.

        Returns:
            True if Jaccard similarity of key-point sets >= CONVERGENCE_THRESHOLD.
        """
        if len(self._rounds) < 2:
            return False
        prev_kp = self._all_key_points(self._rounds[-2])
        curr_kp = self._all_key_points(self._rounds[-1])
        similarity = _jaccard(prev_kp, curr_kp)
        logger.debug(
            "[DebateProtocol] Jaccard similarity round %d vs %d: %.3f (threshold=%.2f)",
            len(self._rounds) - 1,
            len(self._rounds),
            similarity,
            CONVERGENCE_THRESHOLD,
        )
        return similarity >= CONVERGENCE_THRESHOLD

    def finalize(self) -> DebateResult:
        """Close the debate and return synthesized results.

        Computes consensus points (key points present in ALL final-round
        positions), dissenting points (present in some but not all), and
        whether convergence was achieved.

        Returns:
            DebateResult with consensus/dissent analysis.
        """
        if not self._rounds:
            return DebateResult(converged=False, rounds_taken=0)

        converged = self._check_convergence() or len(self._rounds) >= self._max_rounds
        final_round = self._rounds[-1]

        # Compute consensus: key points shared by ALL personas in the final round
        per_persona_sets: list[set[str]] = [{kp.strip().lower() for kp in pos.key_points} for pos in final_round]

        if per_persona_sets:
            consensus = set(per_persona_sets[0])
            for s in per_persona_sets[1:]:
                consensus &= s
            all_points: set[str] = set()
            for s in per_persona_sets:
                all_points |= s
            dissent = all_points - consensus
        else:
            consensus = set()
            dissent = set()

        # Simple recommendation: summarise consensus points
        if consensus:
            recommendation = (
                f"Debate on '{self._topic}' converged after {len(self._rounds)} round(s). "
                f"Agreed: {', '.join(sorted(consensus)[:5])}."
            )
        else:
            recommendation = (
                f"Debate on '{self._topic}' did not reach consensus after "
                f"{len(self._rounds)} round(s). Human review recommended."
            )

        logger.info(
            "[DebateProtocol] Finalized: converged=%s rounds=%d consensus=%d dissent=%d",
            converged,
            len(self._rounds),
            len(consensus),
            len(dissent),
        )

        return DebateResult(
            converged=converged,
            consensus_points=sorted(consensus),
            dissenting_points=sorted(dissent),
            rounds_taken=len(self._rounds),
            final_positions=final_round,
            recommendation=recommendation,
        )
