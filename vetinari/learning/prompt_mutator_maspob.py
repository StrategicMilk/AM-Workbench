"""MASPOB prompt section ordering analyzer."""

from __future__ import annotations

import json
import logging
import re
import threading
from itertools import islice, permutations
from pathlib import Path

from vetinari.boundary_guards import require_score_in_range
from vetinari.constants import VETINARI_STATE_DIR
from vetinari.utils.bounded_collections import BoundedList

logger = logging.getLogger(__name__)
_SECTION_HEADER_RE = re.compile(r"^(#{1,3})\s+(.+)$", re.MULTILINE)


# ---------------------------------------------------------------------------

_DEFAULT_STATE_PATH = VETINARI_STATE_DIR / "maspob_state.json"
_MIN_SAMPLES: int = 10
_MAX_PERMUTATIONS: int = 24  # cap permutation generation for efficiency
_MAX_SECTION_HEADERS: int = 8
_MAX_TRACKED_SECTIONS: int = 64
_MAX_SAMPLES_PER_SLOT: int = 100
_SUBJECT_MARKER_TEMPLATE = re.compile(
    r"(?:^|\b)(?:subject|subject_id|privacy_subject_id|user_id)\s*[=: ]\s*(?P<id>[^\s,;]+)"
)


class MASPOBAnalyzer:
    """Learns optimal prompt section ordering via position-sensitivity analysis.

    Records quality scores for different section orderings and converges
    on the best arrangement.  When sufficient data exists (>= 10 samples),
    provides learned ordering to ``_restructure_format()`` instead of the
    static MASPOB heuristic.

    Args:
        state_path: Path to the JSON file used for persistent state.
    """

    def __init__(self, state_path: Path | None = None) -> None:
        self._state_path = state_path or _DEFAULT_STATE_PATH
        # Maps section_name → {position_str → [quality_scores]}
        self._position_stats: dict[str, dict[str, list[float]]] = {}
        self._lock = threading.Lock()
        self._load_state()

    # ── Public methods ────────────────────────────────────────────────

    def analyze_section_ordering(self, prompt: str) -> list[list[str]]:
        """Generate candidate section orderings (permutations) from a prompt.

        Parses section headers from *prompt* and returns up to
        ``_MAX_PERMUTATIONS`` distinct orderings.

        Args:
            prompt: The prompt text containing markdown-style section headers.

        Returns:
            List of section-name lists representing candidate orderings.
        """
        # Reuse the module-level _SECTION_HEADER_RE (group 2 is the title text)
        section_names = [m.group(2).strip() for m in _SECTION_HEADER_RE.finditer(prompt)][:_MAX_SECTION_HEADERS]
        if not section_names:
            return []
        # Cap permutations to avoid combinatorial explosion
        all_perms = list(islice(permutations(section_names), _MAX_PERMUTATIONS))
        return [list(p) for p in all_perms]

    def record_quality(self, section_name: str, position: int, quality_score: float) -> None:
        """Record an observed quality score for a section at a given position.

        Args:
            section_name: The name of the prompt section.
            position: Zero-based position index in the ordering.
            quality_score: Quality metric (higher is better, e.g. 0.0-1.0).

        Raises:
            ValueError: If ``position`` is negative or ``quality_score`` is outside [0.0, 1.0].
        """
        if position < 0:
            raise ValueError("position must be non-negative")
        quality_score = require_score_in_range(
            quality_score,
            "maspob.position_quality",
            field_name="quality_score",
        )
        section_name = section_name.strip()
        if not section_name:
            raise ValueError("section_name must be non-empty")
        pos_key = str(position)
        with self._lock:
            if section_name not in self._position_stats:
                if len(self._position_stats) >= _MAX_TRACKED_SECTIONS:
                    del self._position_stats[next(iter(self._position_stats))]
                self._position_stats[section_name] = {}
            section_stats = self._position_stats[section_name]
            if pos_key not in section_stats:
                section_stats[pos_key] = []
            samples = BoundedList[float](_MAX_SAMPLES_PER_SLOT, section_stats[pos_key])
            samples.append(quality_score)
            section_stats[pos_key] = list(samples)
            self._save_state_locked()

    def export_subject_data(self, subject: str) -> dict[str, object]:
        """Export MASPOB section stats explicitly tied to ``subject``.

        Returns:
            Export payload containing matching MASPOB records.
        """
        marker = subject.strip()
        records = []
        with self._lock:
            for section_name, stats in self._position_stats.items():
                if _section_marks_subject(section_name, marker):
                    records.append({"section_name": section_name, "position_stats": dict(stats)})
        return {"records": records}

    def records_for_subject(self, subject: str) -> list[dict[str, object]]:
        """Return raw MASPOB records that carry an explicit subject marker.

        Returns:
            List of matching MASPOB records.
        """
        payload = self.export_subject_data(subject)
        return list(payload["records"])  # type: ignore[index]

    def delete_records_for_subject(self, subject: str) -> int:
        """Delete MASPOB section stats carrying an explicit subject marker.

        Returns:
            Number of deleted MASPOB records.
        """
        marker = subject.strip()
        if not marker:
            return 0
        with self._lock:
            doomed = [name for name in self._position_stats if _section_marks_subject(name, marker)]
            for name in doomed:
                del self._position_stats[name]
            if doomed:
                self._save_state_locked()
            return len(doomed)

    def get_optimal_ordering(self, section_names: list[str]) -> list[str]:
        """Return the best section ordering based on accumulated quality data.

        Uses a greedy Hungarian-style algorithm: iteratively assigns each
        section to the available position with its highest average quality
        score.

        Args:
            section_names: Sections to order.

        Returns:
            Ordered list of section names.  Falls back to input order if
            insufficient data for any section.
        """
        if not self.has_sufficient_data(section_names):
            return list(section_names)

        n = len(section_names)
        available_positions = set(range(n))
        ordered: BoundedList[tuple[int, str]] = BoundedList(_MAX_SECTION_HEADERS)  # (position, section_name)

        with self._lock:
            remaining = list(section_names)
            while remaining and available_positions:
                best_section: str | None = None
                best_pos: int | None = None
                best_avg: float = -1.0

                for sec in remaining:
                    stats = self._position_stats.get(sec, {})
                    for pos in available_positions:
                        scores = stats.get(str(pos), [])
                        if scores:
                            avg = sum(scores) / len(scores)
                            if avg > best_avg:
                                best_avg = avg
                                best_section = sec
                                best_pos = pos

                if best_section is None or best_pos is None:
                    # No data for any remaining pair — append in input order
                    for sec in remaining:
                        pos = min(available_positions)
                        ordered.append((pos, sec))
                        available_positions.discard(pos)
                    break

                ordered.append((best_pos, best_section))
                remaining.remove(best_section)
                available_positions.discard(best_pos)

        ordered_items = sorted(ordered, key=lambda x: x[0])
        return [sec for _, sec in ordered_items]

    def has_sufficient_data(self, section_names: list[str]) -> bool:
        """Return True when every (section, position) slot has sufficient evidence.

        The learned ordering is only used when *each* (section, position) pair has
        at least ``_MIN_SAMPLES`` observations.  A section with many samples
        concentrated at one position still lacks evidence for the others — the
        greedy assignment in ``get_optimal_ordering()`` needs per-slot confidence
        to produce a trustworthy result.

        Args:
            section_names: Section names to check. Position slots are
                ``range(len(section_names))``, mirroring ``get_optimal_ordering()``.

        Returns:
            True when every (section, position) pair has ``_MIN_SAMPLES``
            observations, False otherwise (falls back to heuristic ordering).
        """
        n = len(section_names)
        with self._lock:
            for name in section_names:
                stats = self._position_stats.get(name, {})
                for pos in range(n):
                    if len(stats.get(str(pos), [])) < _MIN_SAMPLES:
                        return False
        return True

    # ── Persistence ───────────────────────────────────────────────────

    def _load_state(self) -> None:
        """Load persisted position stats from disk, if available."""
        try:
            if self._state_path.exists():
                with self._state_path.open("r", encoding="utf-8") as fh:
                    self._position_stats = json.load(fh)
        except Exception as exc:
            logger.warning("MASPOBAnalyzer: failed to load state from %s: %s", self._state_path, exc)
            self._position_stats = {}

    def _save_state(self) -> None:
        """Persist position stats to disk."""
        with self._lock:
            self._save_state_locked()

    def _save_state_locked(self) -> None:
        """Persist position stats to disk while self._lock is already held."""
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            data = dict(self._position_stats)
            with self._state_path.open("w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
        except Exception as exc:
            logger.warning("MASPOBAnalyzer: failed to save state to %s: %s", self._state_path, exc)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_maspob_analyzer: MASPOBAnalyzer | None = None
_maspob_lock: threading.Lock = threading.Lock()


def get_maspob_analyzer(state_path: Path | None = None) -> MASPOBAnalyzer:
    """Return the module-level singleton :class:`MASPOBAnalyzer`.

    When ``state_path`` is provided the singleton is (re-)created with that
    path.  This allows tests to inject a temporary directory without relying
    on the process-wide default.

    Args:
        state_path: Optional override for the state file path.  Passing a
            non-None value always creates a fresh instance, even if one
            already exists.

    Returns:
        The singleton :class:`MASPOBAnalyzer` instance.
    """
    global _maspob_analyzer
    if _maspob_analyzer is None or state_path is not None:
        with _maspob_lock:
            if _maspob_analyzer is None or state_path is not None:
                _maspob_analyzer = MASPOBAnalyzer(state_path=state_path)
    return _maspob_analyzer


def _section_marks_subject(section_name: str, subject: str) -> bool:
    if not subject:
        return False
    if section_name == subject:
        return True
    return any(match.group("id") == subject for match in _SUBJECT_MARKER_TEMPLATE.finditer(section_name))
