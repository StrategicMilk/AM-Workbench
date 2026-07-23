"""Inspectable RAG retrieval lab for Workbench dataset revisions."""

from __future__ import annotations

import importlib
import json
import logging
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vetinari.agents.contracts import OutcomeSignal, Provenance, ToolEvidence
from vetinari.constants import OUTPUTS_DIR
from vetinari.embeddings import get_embedder
from vetinari.receipts.record import WorkReceipt, WorkReceiptKind
from vetinari.receipts.store import WorkReceiptStore
from vetinari.types import AgentType, EvidenceBasis, ShardKind
from vetinari.workbench.evals import EvalKind, EvalResult, EvalScore
from vetinari.workbench.rag_debugger_records import (
    ContextAssembly,
    RagDebuggerError,
    RagEmbeddingModelMismatch,
    RagFaithfulnessVerdict,
    RagIndexMissing,
    RagQueryTooLarge,
    RerankBreakdown,
    RetrievalCandidate,
    RetrievalLabExperiment,
    RetrievalQuery,
    RetrievalTrace,
    _validate_project_id,
)

logger = logging.getLogger(__name__)

_SUBMODULE_DIR = Path(__file__).with_suffix("")
if _SUBMODULE_DIR.is_dir():
    __path__: list[str] = [str(_SUBMODULE_DIR)]

_payloads = importlib.import_module("vetinari.workbench.rag_debugger.payloads")
experiment_from_payload = _payloads.experiment_from_payload
to_jsonable = _payloads.to_jsonable
_experiments_store = importlib.import_module("vetinari.workbench.rag_debugger.experiments_store")
append_experiment_record = _experiments_store.append_experiment_record
_scoring = importlib.import_module("vetinari.workbench.rag_debugger.retrieval_scoring")
_assemble_context = _scoring._assemble_context
_chunk_metadata = _scoring._chunk_metadata
_cosine_similarity = _scoring._cosine_similarity
_expanded_query_terms = _scoring._expanded_query_terms
_is_relative_to = _scoring._is_relative_to
_maybe_get = _scoring._maybe_get
_terms = _scoring._terms
_faithfulness_judge = importlib.import_module("vetinari.workbench.rag_debugger.faithfulness_judge")
_FAITHFULNESS_SYSTEM_PROMPT = _faithfulness_judge._FAITHFULNESS_SYSTEM_PROMPT
_FAITHFULNESS_TASK_TYPE = _faithfulness_judge._FAITHFULNESS_TASK_TYPE
_invoke_faithfulness_judge_impl = _faithfulness_judge._invoke_faithfulness_judge
_judge_context = _faithfulness_judge._judge_context
_resolve_faithfulness_model_id_impl = _faithfulness_judge._resolve_faithfulness_model_id
_score_faithfulness_llm_impl = _faithfulness_judge._score_faithfulness_llm
RAG_DEBUGGER_PUBLIC_EXPORTS = _faithfulness_judge.RAG_DEBUGGER_PUBLIC_EXPORTS


_EXPERIMENT_FILENAME = "experiments.jsonl"
_DEFAULT_LAB_DIR = OUTPUTS_DIR / "workbench" / "rag_debugger"
_MAX_QUERY_BYTES = 4096
_SCHEMA_VERSION = 1


class RagRetrievalLab:
    """RAG inspection facade over dataset revisions and local experiments."""

    def __init__(
        self,
        *,
        base_dir: Path | str = _DEFAULT_LAB_DIR,
        project_id: str = "default",
        dataset_loader: Callable[[str], Any] | None = None,
        receipt_store: WorkReceiptStore | None = None,
    ) -> None:
        _validate_project_id(project_id)
        root = Path(base_dir).expanduser().resolve()
        self._base_dir = root
        self._project_id = project_id
        self._dataset_loader = dataset_loader if dataset_loader is not None else self._load_revision_from_store
        self._receipt_store = receipt_store if receipt_store is not None else WorkReceiptStore()
        self._write_lock = threading.Lock()

    def inspect_dataset(self, revision_id: str) -> dict[str, Any]:
        """Return an operator-readable summary of a revision's retrieval index.

        Returns:
            dict[str, Any] value produced by inspect_dataset().
        """
        revision = self._dataset_loader(revision_id)
        index = _extract_index(revision)
        chunks = _extract_chunks(index)
        return {
            "revision_id": revision_id,
            "collection": _extract_index_value(index, "collection", "default"),
            "embedding_model": _extract_index_value(index, "embedding_model", ""),
            "chunk_count": len(chunks),
            "metadata_keys": sorted({key for chunk in chunks for key in _chunk_metadata(chunk)}),
        }

    def list_datasets(self) -> list[dict[str, Any]]:
        """Return dataset revisions known to the seeded revision store.

        Returns:
            Collection of datasets values.
        """
        try:
            from vetinari.workbench.dataset_revisions import get_dataset_revision_store

            store = get_dataset_revision_store()
            revisions = store.list_revisions()
            branches = {branch.head_revision_id: branch.name for branch in store.list_branches()}
        except Exception:
            logger.warning("Handled recoverable failure before fallback.", exc_info=True)
            return []
        return [
            {
                "revision_id": revision.revision_id,
                "branch": branches.get(revision.revision_id, revision.branch),
                "status": revision.status.value,
                "assets": len(revision.assets),
                "promotion_gate": to_jsonable(store.gate_for_promotion(revision.revision_id)),
            }
            for revision in revisions
        ]

    def replay_query(
        self, revision_id: str, query: RetrievalQuery
    ) -> tuple[
        RetrievalTrace,
        tuple[RerankBreakdown, ...],
        ContextAssembly,
        RagFaithfulnessVerdict,
    ]:
        """Replay a query against a revision and return every inspection surface.

        Args:
            revision_id: Revision id value consumed by replay_query().
            query: Query value consumed by replay_query().

        Returns:
            tuple[RetrievalTrace, tuple[RerankBreakdown, ...], ContextAssembly, RagFaithfulnessVerdict] value produced by replay_query().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        _raise_if_query_too_large(query)
        revision = self._dataset_loader(revision_id)
        index = _extract_index(revision)
        chunks = _extract_chunks(index)
        index_model = str(_extract_index_value(index, "embedding_model", ""))
        if index_model and query.embedding_model and index_model != query.embedding_model:
            raise RagEmbeddingModelMismatch(
                f"Embedding model mismatch: query {query.embedding_model}, index {index_model}"
            )

        candidates, rejected = self._score_chunks(chunks, query)
        selected = tuple(sorted(candidates, key=lambda item: item.rerank_score, reverse=True)[: query.top_k])
        score_rejected = tuple(
            candidate for candidate in candidates if candidate not in selected and candidate.score < query.min_score
        )
        rejected = rejected + tuple(
            RetrievalCandidate(
                chunk_id=candidate.chunk_id,
                document_id=candidate.document_id,
                text=candidate.text,
                score=candidate.score,
                dense_score=candidate.dense_score,
                sparse_score=candidate.sparse_score,
                rerank_score=candidate.rerank_score,
                metadata=candidate.metadata,
                rejected=True,
                rejection_reason="score_below_threshold",
            )
            for candidate in score_rejected
        )
        filters = tuple(f"{key}={value}" for key, value in sorted(query.filters.items()))
        trace = RetrievalTrace(
            revision_id=revision_id,
            query=query,
            candidates=selected,
            rejected_candidates=rejected,
            filters_applied=filters,
            reranker=query.reranker,
            embedding_model=query.embedding_model,
            index_embedding_model=index_model,
            collection=str(_extract_index_value(index, "collection", "default")),
        )
        breakdown = tuple(
            RerankBreakdown(
                candidate_id=candidate.chunk_id,
                pre_rerank_score=candidate.score,
                post_rerank_score=candidate.rerank_score,
                delta=candidate.rerank_score - candidate.score,
                dense_weight=query.hybrid_alpha,
                sparse_weight=1.0 - query.hybrid_alpha,
                metadata_filters=filters,
            )
            for candidate in selected
        )
        context = _assemble_context(selected, rejected)
        verdict = _judge_context(query, context)
        return trace, breakdown, context, verdict

    def save_experiment(self, experiment: RetrievalLabExperiment) -> RetrievalLabExperiment:
        """Append one experiment JSONL row and emit exactly one SPINE_EVENT receipt.

        Returns:
            RetrievalLabExperiment value produced by save_experiment().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        path = self._lab_dir(experiment.project_id) / _EXPERIMENT_FILENAME
        with self._write_lock:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                append_experiment_record(
                    path,
                    to_jsonable(experiment),
                    schema_version=_SCHEMA_VERSION,
                    kind="retrieval_lab_experiment",
                )
                self._receipt_store.append(self._receipt_for(experiment))
                try:
                    from vetinari.workbench import spine_consumers

                    spine_consumers.get_spine()
                    spine_consumers.record_asset_written(
                        experiment.experiment_id,
                        "rag_debugger_experiment",
                        experiment.project_id,
                        path=str(path),
                        redact_fields=["path"],
                    )
                except Exception:
                    logger.warning("RAG debugger spine consumer record failed", exc_info=True)
            except OSError as exc:
                raise RagDebuggerError(f"experiment append failed for {path}") from exc
        return experiment

    def list_experiments(self, *, project_id: str | None = None) -> tuple[RetrievalLabExperiment, ...]:
        """Read saved experiments, failing closed on corrupted JSONL.

        Returns:
            Collection of experiments values.

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        resolved_project = project_id or self._project_id
        path = self._lab_dir(resolved_project) / _EXPERIMENT_FILENAME
        if not path.exists():
            return ()
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise RagDebuggerError(f"experiment log unreadable: {path}") from exc
        if raw and not raw.endswith(b"\n"):
            raise RagDebuggerError(f"experiment log truncated: {path}")
        rows: list[RetrievalLabExperiment] = []
        for lineno, line in enumerate(raw.decode("utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RagDebuggerError(f"experiment log parse failed at line {lineno}") from exc
            if row.get("schema_version") != _SCHEMA_VERSION or row.get("kind") != "retrieval_lab_experiment":
                raise RagDebuggerError(f"experiment log invalid row at line {lineno}")
            rows.append(experiment_from_payload(row["payload"]))
        return tuple(rows)

    def promote_experiment_to_eval_case(self, experiment_id: str) -> EvalResult:
        """Convert a saved failed experiment into a live-trace-derived eval case.

        Returns:
            EvalResult value produced by promote_experiment_to_eval_case().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        experiment = next(
            (row for row in self.list_experiments() if row.experiment_id == experiment_id),
            None,
        )
        if experiment is None:
            raise RagDebuggerError(f"experiment {experiment_id!r} not found")
        failed = 0.0 if experiment.verdict.passed else 1.0
        return EvalResult(
            eval_id=f"rag-eval-{uuid.uuid4().hex[:12]}",
            kind=EvalKind.LIVE_TRACE_DERIVED,
            run_id=experiment.trace.revision_id,
            asset_id=experiment.revision_id,
            asset_revision=experiment.revision_id,
            scores=(
                EvalScore(
                    metric_name="rag_debugger_failure",
                    value=failed,
                    threshold=0.0,
                    passed=experiment.verdict.passed,
                    unit="bool",
                ),
            ),
            captured_at_utc=_utc_now_iso(),
            notes=f"promoted from retrieval lab experiment {experiment.experiment_id}",
        )

    def build_experiment(
        self,
        revision_id: str,
        query: RetrievalQuery,
        *,
        notes: str = "",
    ) -> RetrievalLabExperiment:
        """Replay a query and package the result as a persistable experiment.

        Args:
            revision_id: Revision id value consumed by build_experiment().
            query: Query value consumed by build_experiment().
            notes: Notes value consumed by build_experiment().

        Returns:
            Newly constructed experiment value.
        """
        trace, breakdown, context, verdict = self.replay_query(revision_id, query)
        return RetrievalLabExperiment(
            experiment_id=f"rag-exp-{uuid.uuid4().hex[:12]}",
            project_id=self._project_id,
            revision_id=revision_id,
            query=query,
            trace=trace,
            rerank_breakdown=breakdown,
            context_assembly=context,
            verdict=verdict,
            created_at_utc=_utc_now_iso(),
            notes=notes,
        )

    def _lab_dir(self, project_id: str) -> Path:
        _validate_project_id(project_id)
        root = self._base_dir.resolve()
        candidate = (root / project_id).resolve()
        if not _is_relative_to(candidate, root):
            raise RagDebuggerError(f"project_id {project_id!r} escapes rag debugger root")
        return candidate

    @staticmethod
    def _score_chunks(
        chunks: list[Any],
        query: RetrievalQuery,
    ) -> tuple[tuple[RetrievalCandidate, ...], tuple[RetrievalCandidate, ...]]:
        candidates: list[RetrievalCandidate] = []
        rejected: list[RetrievalCandidate] = []
        query_terms = _expanded_query_terms(query)
        for order, chunk in enumerate(chunks):
            metadata = _chunk_metadata(chunk)
            base_candidate = _candidate_from_chunk(chunk, order, query_terms, query)
            missing_filter = next(
                (
                    f"{key}={value}"
                    for key, value in sorted(query.filters.items())
                    if str(metadata.get(key, "")) != value
                ),
                "",
            )
            if missing_filter:
                rejected.append(
                    RetrievalCandidate(
                        chunk_id=base_candidate.chunk_id,
                        document_id=base_candidate.document_id,
                        text=base_candidate.text,
                        score=base_candidate.score,
                        dense_score=base_candidate.dense_score,
                        sparse_score=base_candidate.sparse_score,
                        rerank_score=base_candidate.rerank_score,
                        metadata=base_candidate.metadata,
                        rejected=True,
                        rejection_reason=f"metadata_filter:{missing_filter}",
                    )
                )
                continue
            candidates.append(base_candidate)
        return tuple(candidates), tuple(rejected)

    @staticmethod
    def _load_revision_from_store(revision_id: str) -> Any:
        from vetinari.workbench.dataset_revisions import get_dataset_revision_store

        store = get_dataset_revision_store()
        revisions = getattr(store, "_revisions", None)
        if isinstance(revisions, dict):
            revision = revisions.get(revision_id)
            if revision is not None:
                return revision
            raise RagIndexMissing(f"dataset revision {revision_id!r} not found")
        for revision in store.list_revisions():
            if revision.revision_id == revision_id:
                return revision
        raise RagIndexMissing(f"dataset revision {revision_id!r} not found")

    @staticmethod
    def _assert_training_run_carries_dataset_revision(run: Any, provenance: Any) -> None:
        from vetinari.workbench.dataset_revisions import get_dataset_revision_store

        get_dataset_revision_store().assert_training_run_carries_dataset_revision(run, provenance)

    @staticmethod
    def _receipt_for(experiment: RetrievalLabExperiment) -> WorkReceipt:
        now = _utc_now_iso()
        return WorkReceipt(
            project_id=experiment.project_id,
            agent_id="workbench-rag-debugger",
            agent_type=AgentType.WORKBENCH,
            kind=WorkReceiptKind.SPINE_EVENT,
            outcome=OutcomeSignal(
                passed=True,
                score=1.0,
                basis=EvidenceBasis.TOOL_EVIDENCE,
                tool_evidence=(
                    ToolEvidence(
                        tool_name="rag_retrieval_lab",
                        command="save_experiment",
                        exit_code=0,
                        stdout_snippet=f"experiment_id={experiment.experiment_id}",
                        passed=True,
                    ),
                ),
                provenance=Provenance(
                    source="vetinari.workbench.rag_debugger",
                    timestamp_utc=now,
                    tool_name="rag_retrieval_lab",
                ),
                kind=ShardKind.STANDARD,
            ),
            started_at_utc=now,
            finished_at_utc=now,
            inputs_summary=f"rag experiment save: {experiment.revision_id}",
            outputs_summary=f"experiment_id={experiment.experiment_id}",
        )


_INSTANCE: RagRetrievalLab | None = None
_INSTANCE_LOCK = threading.Lock()


def get_rag_retrieval_lab() -> RagRetrievalLab:
    """Return the process-wide RAG retrieval lab singleton.

    Returns:
        Resolved rag retrieval lab value.
    """
    global _INSTANCE
    if _INSTANCE is None:
        with _INSTANCE_LOCK:
            if _INSTANCE is None:
                _INSTANCE = RagRetrievalLab()
    return _INSTANCE


def reset_rag_retrieval_lab_for_test() -> None:
    """Clear the process-wide RAG retrieval lab singleton for isolated tests."""
    global _INSTANCE
    with _INSTANCE_LOCK:
        _INSTANCE = None


def _raise_if_query_too_large(query: RetrievalQuery) -> None:
    size = len(query.query_text.encode("utf-8"))
    if size > _MAX_QUERY_BYTES:
        raise RagQueryTooLarge(f"Query too large: {size} bytes exceeds limit {_MAX_QUERY_BYTES}")


def _extract_index(revision: Any) -> Any:
    index = _maybe_get(revision, "retrieval_index", None)
    if index is None:
        index = _maybe_get(revision, "index", None)
    if index is None:
        raise RagIndexMissing("dataset revision has no retrieval index")
    return index


def _extract_chunks(index: Any) -> list[Any]:
    chunks = _extract_index_value(index, "chunks", None)
    if chunks is None:
        chunks = _extract_index_value(index, "candidates", None)
    if not chunks:
        raise RagIndexMissing("dataset revision retrieval index has no chunks")
    return list(chunks)


def _extract_index_value(index: Any, key: str, default: Any) -> Any:
    return _maybe_get(index, key, default)


def _candidate_from_chunk(
    chunk: Any,
    order: int,
    query_terms: set[str],
    query: RetrievalQuery,
) -> RetrievalCandidate:
    return _scoring._candidate_from_chunk(
        chunk,
        order,
        query_terms,
        query,
        score_rerank=_score_rerank_candidate,
    )


def _score_rerank_candidate(query: RetrievalQuery, candidate_text: str, base_score: float) -> float:
    if not query.reranker or query.reranker == "none":
        return base_score
    try:
        query_vector, candidate_vector = get_embedder().embed_batch([query.query_text, candidate_text])
    except Exception as exc:
        raise RagDebuggerError(f"RAG reranker unavailable: {query.reranker}") from exc
    similarity = _cosine_similarity(query_vector, candidate_vector)
    normalized_similarity = (similarity + 1.0) / 2.0
    return base_score + (normalized_similarity * 0.1)


def _score_faithfulness_llm(query_text: str, context_text: str, *, default_score: float) -> float:
    """Score RAG faithfulness through the configured LLM judge profile."""
    return _score_faithfulness_llm_impl(
        query_text,
        context_text,
        default_score=default_score,
        invoke_judge=_invoke_faithfulness_judge,
        logger=logger,
    )


def _invoke_faithfulness_judge(prompt: str) -> dict[str, Any]:
    return _invoke_faithfulness_judge_impl(prompt)


def _resolve_faithfulness_model_id(adapter: Any) -> str:
    return _resolve_faithfulness_model_id_impl(adapter, logger=logger)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = RAG_DEBUGGER_PUBLIC_EXPORTS
