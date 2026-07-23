"""Intent-aware query parser for memory retrieval — routes natural language to the right search strategy.

Inspired by OBDA (Ontology-Based Data Access): agents ask questions in natural language
and the system routes to the appropriate search backend automatically. No need to know
whether to use FTS5, KNN, timeline, or episode recall.

This is a pure pattern-matching parser with no LLM dependency — it uses compiled
regex patterns and keyword lists to classify queries in microseconds.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

logger = logging.getLogger(__name__)


# -- Stop words excluded from keyword extraction --
_STOP_WORDS: frozenset[str] = frozenset({
    "a",
    "an",
    "the",
    "and",
    "or",
    "but",
    "in",
    "on",
    "at",
    "to",
    "for",
    "of",
    "with",
    "by",
    "from",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "being",
    "have",
    "has",
    "had",
    "do",
    "does",
    "did",
    "will",
    "would",
    "could",
    "should",
    "may",
    "might",
    "shall",
    "can",
    "not",
    "no",
    "nor",
    "so",
    "yet",
    "both",
    "either",
    "neither",
    "about",
    "above",
    "after",
    "before",
    "between",
    "during",
    "into",
    "through",
    "what",
    "when",
    "where",
    "which",
    "who",
    "whom",
    "that",
    "this",
    "these",
    "those",
    "i",
    "me",
    "my",
    "we",
    "our",
    "you",
    "your",
    "it",
    "its",
    "they",
    "them",
    "their",
    "went",
    "going",
    "get",
    "got",
    "go",
})

# -- Minimum keyword length to include in extraction --
_MIN_KEYWORD_LEN = 3


class QueryIntent(Enum):
    """Classification of a memory query into a retrieval strategy.

    Each value maps to a different backend call in UnifiedMemoryStore.
    """

    EPISODE_RECALL = "episode_recall"  # "what went wrong with X?"
    TIMELINE = "timeline"  # "what happened last week?"
    KNOWLEDGE_BASE = "knowledge_base"  # "what are the rules about X?"
    SEMANTIC_SEARCH = "semantic_search"  # general fallback


@dataclass(frozen=True, slots=True)
class ParsedQuery:
    """The result of parsing a natural language memory query.

    All fields are immutable after construction — ParsedQuery is a value object.

    Attributes:
        intent: Which retrieval backend to use.
        original_query: The raw input string, preserved verbatim.
        keywords: Meaningful terms extracted after stop-word removal.
        time_range: ``(start_epoch_ms, end_epoch_ms)`` or ``(None, None)`` when
            no time reference was detected. Epochs are milliseconds UTC.
        task_type: Task category extracted from an episode-recall query
            (e.g. ``"caching"``, ``"inference"``).
        success_filter: ``True`` → successful episodes only, ``False`` →
            failures only, ``None`` → no filter.
        topic: Subject phrase extracted from knowledge-base queries.
    """

    intent: QueryIntent
    original_query: str
    keywords: list[str]
    time_range: tuple[int | None, int | None]
    task_type: str | None
    success_filter: bool | None
    topic: str | None

    def __repr__(self) -> str:
        return (
            f"ParsedQuery(intent={self.intent.value!r}, keywords={self.keywords!r}, "
            f"task_type={self.task_type!r}, topic={self.topic!r})"
        )


# -- Patterns that signal episode recall (failure) --
_FAILURE_SIGNALS: tuple[str, ...] = (
    "went wrong",
    "failed",
    "fail",
    "broke",
    "broken",
    "error",
    "bug",
    "mistake",
    "problem with",
    "issue with",
    "crashed",
    "crash",
    "exception",
    "failure",
    "not working",
)

# -- Patterns that signal episode recall (success) --
_SUCCESS_SIGNALS: tuple[str, ...] = (
    "worked well",
    "succeeded",
    "good results",
    "successful",
    "passed",
    "completed successfully",
    "worked correctly",
)

# -- Patterns that signal a timeline query --
_TIMELINE_SIGNALS: tuple[str, ...] = (
    "last week",
    "yesterday",
    "recently",
    "last month",
    "today",
    "this week",
    "when did",
    "history of",
    "last few days",
    "past week",
    "past month",
    "past few",
    "earlier today",
    "earlier this",
)

# -- Patterns that signal a knowledge-base query --
_KB_SIGNALS: tuple[str, ...] = (
    "rules about",
    "how to",
    "what is the",
    "documentation",
    "standard for",
    "convention",
    "policy",
    "guideline",
    "best practice",
    "what are the rules",
    "how do i",
    "what should",
    "correct way",
)

# -- Regex for time reference extraction (compiled once at module load) --
_RE_LAST_WEEK = re.compile(r"\b(?:last\s+week|past\s+week)\b", re.IGNORECASE)
_RE_YESTERDAY = re.compile(r"\byesterday\b", re.IGNORECASE)
_RE_LAST_MONTH = re.compile(r"\b(?:last\s+month|past\s+month)\b", re.IGNORECASE)
_RE_RECENTLY = re.compile(r"\b(?:recently|last\s+few\s+days|past\s+few\s+days)\b", re.IGNORECASE)
_RE_TODAY = re.compile(r"\b(?:today|earlier\s+today)\b", re.IGNORECASE)
_RE_THIS_WEEK = re.compile(r"\bthis\s+week\b", re.IGNORECASE)

# -- Regex for extracting topic after KB signal phrases --
_RE_KB_TOPIC = re.compile(
    r"(?:rules about|how to|what is the|standard for|convention for|"
    r"policy (?:on|for|about)|guideline(?:s)? (?:on|for|about)|"
    r"best practice(?:s)? (?:for|on|about)|how do i|what should i|"
    r"correct way to)\s+(.+?)(?:\?|$)",
    re.IGNORECASE,
)

# -- Regex to extract task type keywords from episode queries --
_RE_TASK_TYPE = re.compile(
    r"\b(caching|inference|routing|training|indexing|embedding|search|"
    r"parsing|validation|compilation|deployment|scheduling|queuing|"
    r"streaming|batching|summarization|retrieval|generation|planning|"
    r"execution|evaluation|scoring|classification|extraction|encoding)\b",
    re.IGNORECASE,
)

# -- Non-word characters used as token separators in keyword extraction --
_RE_NONWORD = re.compile(r"[^\w\s]")


class IntentParser:
    """Classifies natural language memory queries into retrieval strategies.

    Uses compiled patterns and keyword sets for classification — no external
    calls, no LLM, deterministic output for the same input.

    Instantiate once at module level via :func:`get_intent_parser`.
    """

    def parse(self, query: str) -> ParsedQuery:
        """Classify *query* and extract retrieval parameters.

        Checks patterns in priority order: episode recall, timeline,
        knowledge base, then falls back to semantic search.

        Args:
            query: Natural language question from an agent.

        Returns:
            A :class:`ParsedQuery` with intent and extracted parameters.
        """
        lower = query.lower().strip()

        keywords = self._extract_keywords(query)
        time_range = self._parse_time_range(lower)

        # -- Priority 1: knowledge base (documentation / rule lookup) --
        # KB detection runs first because KB queries often contain incidental
        # words like "error" or "failure" that would otherwise trip episode recall.
        topic = self._extract_topic(lower)
        if topic is not None:
            logger.debug("Query classified as KNOWLEDGE_BASE (topic=%r)", topic)
            return ParsedQuery(
                intent=QueryIntent.KNOWLEDGE_BASE,
                original_query=query,
                keywords=keywords,
                time_range=(None, None),
                task_type=None,
                success_filter=None,
                topic=topic,
            )

        # -- Priority 2: episode recall (failure or success pattern) --
        success_filter, is_episode = self._detect_episode_intent(lower)
        if is_episode:
            task_type = self._extract_task_type(lower)
            logger.debug(
                "Query classified as EPISODE_RECALL (success_filter=%s, task_type=%s)",
                success_filter,
                task_type,
            )
            return ParsedQuery(
                intent=QueryIntent.EPISODE_RECALL,
                original_query=query,
                keywords=keywords,
                time_range=time_range,
                task_type=task_type,
                success_filter=success_filter,
                topic=None,
            )

        # -- Priority 3: timeline (temporal reference detected) --
        if time_range != (None, None) or self._has_timeline_signal(lower):
            logger.debug("Query classified as TIMELINE (time_range=%s)", time_range)
            return ParsedQuery(
                intent=QueryIntent.TIMELINE,
                original_query=query,
                keywords=keywords,
                time_range=time_range,
                task_type=None,
                success_filter=None,
                topic=None,
            )

        # -- Priority 4: semantic search fallback --
        logger.debug("Query classified as SEMANTIC_SEARCH (no specific signals matched)")
        return ParsedQuery(
            intent=QueryIntent.SEMANTIC_SEARCH,
            original_query=query,
            keywords=keywords,
            time_range=(None, None),
            task_type=None,
            success_filter=None,
            topic=None,
        )

    @staticmethod
    def _detect_episode_intent(lower: str) -> tuple[bool | None, bool]:
        """Check whether the query asks about past episodes.

        Checks failure signals first, then success signals.

        Args:
            lower: Lower-cased query string.

        Returns:
            A tuple of ``(success_filter, is_episode)`` where ``success_filter``
            is ``True`` for success queries, ``False`` for failure queries, and
            ``None`` if not an episode query. ``is_episode`` indicates whether
            any episode signal was matched.
        """
        for signal in _FAILURE_SIGNALS:
            if signal in lower:
                return False, True
        for signal in _SUCCESS_SIGNALS:
            if signal in lower:
                return True, True
        return None, False

    @staticmethod
    def _has_timeline_signal(lower: str) -> bool:
        """Return True when *lower* contains any timeline keyword.

        Args:
            lower: Lower-cased query string.

        Returns:
            True if any timeline signal phrase is present.
        """
        return any(signal in lower for signal in _TIMELINE_SIGNALS)

    def _extract_keywords(self, query: str) -> list[str]:
        """Extract meaningful tokens by removing stop words and punctuation.

        Args:
            query: Raw query string (case preserved for extraction).

        Returns:
            List of lower-cased tokens with length >= 3 that are not stop words.
        """
        cleaned = _RE_NONWORD.sub(" ", query)
        tokens = cleaned.lower().split()
        return [tok for tok in tokens if len(tok) >= _MIN_KEYWORD_LEN and tok not in _STOP_WORDS]

    def _parse_time_range(self, lower: str) -> tuple[int | None, int | None]:
        """Convert time references in *lower* to a UTC epoch millisecond range.

        Checks patterns in descending specificity: last month, last week,
        this week, yesterday, recently, today.

        Args:
            lower: Lower-cased query string.

        Returns:
            ``(start_ms, end_ms)`` or ``(None, None)`` when no time reference
            is detected.
        """
        now = datetime.now(timezone.utc)

        if _RE_LAST_MONTH.search(lower):
            start = now - timedelta(days=30)
            return self._to_epoch_ms(start), self._to_epoch_ms(now)

        if _RE_LAST_WEEK.search(lower):
            start = now - timedelta(days=7)
            return self._to_epoch_ms(start), self._to_epoch_ms(now)

        if _RE_THIS_WEEK.search(lower):
            # Start of the current ISO week (Monday 00:00 UTC)
            days_since_monday = now.weekday()
            start = (now - timedelta(days=days_since_monday)).replace(hour=0, minute=0, second=0, microsecond=0)
            return self._to_epoch_ms(start), self._to_epoch_ms(now)

        if _RE_YESTERDAY.search(lower):
            start = now - timedelta(days=1)
            return self._to_epoch_ms(start), self._to_epoch_ms(now)

        if _RE_RECENTLY.search(lower):
            start = now - timedelta(days=3)
            return self._to_epoch_ms(start), self._to_epoch_ms(now)

        if _RE_TODAY.search(lower):
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            return self._to_epoch_ms(start), self._to_epoch_ms(now)

        return None, None

    @staticmethod
    def _extract_task_type(lower: str) -> str | None:
        """Extract a known task-type keyword from an episode recall query.

        Args:
            lower: Lower-cased query string.

        Returns:
            Lower-cased task type string, or ``None`` when none is detected.
        """
        match = _RE_TASK_TYPE.search(lower)
        if match:
            return match.group(1).lower()
        return None

    @staticmethod
    def _extract_topic(lower: str) -> str | None:
        """Extract the subject phrase from a knowledge-base query.

        First tries the compiled regex for structured KB phrases, then
        checks for bare KB signal keywords and returns the full query
        as the topic.

        Args:
            lower: Lower-cased query string.

        Returns:
            Topic string, or ``None`` when the query is not a KB query.
        """
        match = _RE_KB_TOPIC.search(lower)
        if match:
            return match.group(1).strip()

        # Bare KB signal without a captured group — use the raw query as topic
        for signal in _KB_SIGNALS:
            if signal in lower:
                return lower.strip()

        return None

    @staticmethod
    def _to_epoch_ms(dt: datetime) -> int:
        """Convert a datetime to integer milliseconds since the Unix epoch.

        Args:
            dt: A timezone-aware datetime (UTC recommended).

        Returns:
            Integer milliseconds since 1970-01-01T00:00:00Z.
        """
        return int(dt.timestamp() * 1000)


# -- Module-level singleton --
_parser: IntentParser | None = None


def get_intent_parser() -> IntentParser:
    """Return the module-level IntentParser singleton, creating it on first call.

    The parser is stateless after construction, so sharing a single instance
    across threads is safe.

    Returns:
        The shared :class:`IntentParser` instance.
    """
    global _parser
    if _parser is None:
        _parser = IntentParser()
    return _parser
