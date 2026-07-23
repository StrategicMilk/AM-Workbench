"""Lookup path for SemanticCache."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from vetinari.security.fail_closed import sanitize_untrusted_text
from vetinari.utils.math_helpers import cosine_similarity


class _SemanticCacheLookupMixin:
    """Three-tier semantic cache lookup implementation."""

    if TYPE_CHECKING:
        _default_threshold: Any
        _evict_expired: Any
        _exact_index: Any
        _lock: Any
        _minhash_index: Any
        _store: Any

    def get(
        self,
        query: str,
        similarity_threshold: float = 0.0,
        task_type: str = "",
        model_id: str = "",
        system_prompt: str = "",
    ) -> str | None:
        """Return a cached response if a similar isolated query exists.

        Args:
            query: Query value consumed by get().
            similarity_threshold: Threshold value used to classify the result.
            task_type: Task type value consumed by get().
            model_id: Model identifier used for routing or lookup.
            system_prompt: System prompt value consumed by get().

        Returns:
            Value produced for the caller.
        """
        from vetinari.optimization import semantic_cache as sc

        query = sanitize_untrusted_text(query, max_length=20_000)
        threshold = self._resolve_lookup_threshold(sc, similarity_threshold, task_type)
        system_prompt_ref = sc._cache_context_ref(system_prompt)
        composite_hash = self._composite_query_hash(query, model_id, system_prompt_ref)

        with self._lock:
            self._evict_expired()
            lookup = self._lookup_exact(composite_hash, query, sc)
            if lookup is not None:
                return lookup
            lookup = self._lookup_minhash(query, threshold, model_id, system_prompt_ref, sc)
            if lookup is not None:
                return lookup
            lookup = self._lookup_semantic(query, threshold, model_id, system_prompt_ref, sc)
            if lookup is not None:
                return lookup
            lookup = self._lookup_trigram(query, threshold, model_id, system_prompt_ref, sc)
            if lookup is not None:
                return lookup

            self._misses += 1
            sc._report_cache_miss()
            return None

    def _resolve_lookup_threshold(self, sc: Any, similarity_threshold: float, task_type: str) -> float:
        """Resolve explicit, task-specific, or default semantic-cache threshold."""
        if similarity_threshold > 0.0:
            return similarity_threshold
        if task_type:
            return float(sc.get_threshold_for_task_type(task_type))
        return self._default_threshold

    @staticmethod
    def _composite_query_hash(query: str, model_id: str, system_prompt: str) -> str:
        """Return the exact-tier hash isolated by query, model, and prompt."""
        key_material = f"{query}\x00{model_id}\x00{system_prompt}"
        return hashlib.sha256(key_material.encode("utf-8")).hexdigest()

    def _record_cache_hit(self, key: str, counter_attr: str, tier: str, query: str, sc: Any, score: float = 0.0) -> str:
        """Update hit counters and return the cached response for ``key``."""
        entry = self._store[key]
        entry.hit_count += 1
        self._store.move_to_end(key)
        self._hits += 1
        setattr(self, counter_attr, getattr(self, counter_attr) + 1)
        self._tokens_saved += max(1, len(entry.response) // 4)
        query_ref = hashlib.sha256(query.encode("utf-8")).hexdigest()[:12]
        if score:
            sc.logger.debug("SemanticCache %s HIT (score=%.3f) for query_sha=%s", tier, score, query_ref)
        else:
            sc.logger.debug("SemanticCache %s HIT for query_sha=%s", tier, query_ref)
        sc._report_cache_hit()
        return entry.response

    def _lookup_exact(self, composite_hash: str, query: str, sc: Any) -> str | None:
        """Lookup by exact composite hash."""
        key = self._exact_index.get(composite_hash)
        if key is None or key not in self._store:
            return None
        return self._record_cache_hit(key, "_exact_hits", "EXACT", query, sc)

    def _best_sparse_candidate(
        self,
        candidates: list[str],
        query_embedding: set[str],
        model_id: str,
        system_prompt: str,
        sc: Any,
    ) -> tuple[str | None, float]:
        """Return the best Jaccard candidate matching model and prompt isolation."""
        best_score = 0.0
        best_key: str | None = None
        for candidate_key in candidates:
            if candidate_key not in self._store:
                continue
            cand_entry = self._store[candidate_key]
            if cand_entry.model_id != model_id or cand_entry.system_prompt != system_prompt:
                continue
            score = sc._jaccard(query_embedding, cand_entry.embedding)
            if score > best_score:
                best_score = score
                best_key = candidate_key
        return best_key, best_score

    def _lookup_minhash(self, query: str, threshold: float, model_id: str, system_prompt: str, sc: Any) -> str | None:
        """Lookup through MinHash LSH candidates when datasketch is available."""
        if not (sc._DATASKETCH_AVAILABLE and self._minhash_index is not None and len(self._store) > 0):
            return None
        try:
            candidates = self._minhash_index.query(sc._make_minhash(query))
        except Exception:
            candidates = []
        if not candidates:
            return None
        best_key, best_score = self._best_sparse_candidate(candidates, sc._trigrams(query), model_id, system_prompt, sc)
        if best_key is not None and best_score >= threshold:
            return self._record_cache_hit(best_key, "_minhash_hits", "MINHASH", query, sc, best_score)
        return None

    def _lookup_semantic(self, query: str, threshold: float, model_id: str, system_prompt: str, sc: Any) -> str | None:
        """Lookup by dense embedding cosine similarity when embeddings are available."""
        if not sc._get_embedder_available():
            return None
        query_dense = sc._compute_embedding(query)
        if query_dense is None:
            return None
        best_score = 0.0
        best_key = None
        for key, entry in self._store.items():
            if entry.model_id != model_id or entry.system_prompt != system_prompt or entry.dense_embedding is None:
                continue
            score = cosine_similarity(query_dense, entry.dense_embedding)
            if score > best_score:
                best_score = score
                best_key = key
        if best_key is not None and best_score >= threshold:
            return self._record_cache_hit(best_key, "_semantic_hits", "SEMANTIC", query, sc, best_score)
        return None

    def _lookup_trigram(self, query: str, threshold: float, model_id: str, system_prompt: str, sc: Any) -> str | None:
        """Lookup by trigram Jaccard as the always-available final tier."""
        best_key, best_score = self._best_sparse_candidate(
            list(self._store.keys()),
            sc._trigrams(query),
            model_id,
            system_prompt,
            sc,
        )
        if best_key is not None and best_score >= threshold:
            return self._record_cache_hit(best_key, "_trigram_hits", "TRIGRAM", query, sc, best_score)
        return None
