"""Read-only summaries for full-spectrum audit result runs."""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from vetinari.constants import get_user_dir

logger = logging.getLogger(__name__)

_ROOT = Path(os.environ.get("VETINARI_REPO_ROOT", Path(__file__).resolve().parents[1]))
_DEFAULT_INDEX_PATH = get_user_dir() / "audit" / "RUN-INDEX.json"
_RUN_ID_RE = re.compile(r"[A-Za-z0-9_.-]{1,128}")
_SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


def load_full_spectrum_audit_results(
    *,
    index_path: Path | None = None,
    limit: int = 10,
    include_archived: bool = False,
) -> dict[str, Any]:
    """Load current full-spectrum audit result summaries from on-disk indexes.

    Args:
        index_path: Optional RUN-INDEX path. Defaults to the governed
            per-user audit run index.
        limit: Maximum number of runs to return after filtering.
        include_archived: Whether archived runs should be included.

    Returns:
        A JSON-serializable summary envelope with run metadata and counts.
    """
    resolved_index, index_payload, raw_runs, runs_root = _load_index(index_path)
    bounded_limit = max(1, min(int(limit), 50))
    if not isinstance(index_payload, dict):
        return {
            "status": "unavailable",
            "index_path": _display_path(resolved_index),
            "runs": [],
            "summary": {"total_runs": 0, "visible_runs": 0, "open_findings": 0},
            "error": "full-spectrum audit RUN-INDEX.json is missing or unreadable",
        }

    summaries: list[dict[str, Any]] = []
    skipped = 0
    for raw_run in raw_runs:
        if not isinstance(raw_run, dict):
            skipped += 1
            continue
        if raw_run.get("archived") and not include_archived:
            continue
        run_id = str(raw_run.get("run_id") or "").strip()
        run_root = _safe_run_root(resolved_index, runs_root, run_id, raw_run.get("run_root"))
        if run_root is None:
            skipped += 1
            continue
        summaries.append(_summarize_run(raw_run, run_root))
    summaries.sort(key=lambda row: str(row.get("started_at") or row.get("completed_at") or ""), reverse=True)
    visible = summaries[:bounded_limit]
    return {
        "status": "ok",
        "schema_version": "1.0.0",
        "index_path": _display_path(resolved_index),
        "include_archived": include_archived,
        "limit": bounded_limit,
        "runs": visible,
        "summary": {
            "total_runs": len(raw_runs),
            "visible_runs": len(visible),
            "skipped_runs": skipped,
            "open_findings": sum(int(row.get("open_findings", 0)) for row in visible),
            "total_findings": sum(int(row.get("finding_count", 0)) for row in visible),
        },
    }


def load_full_spectrum_audit_run(
    *,
    run_id: str,
    index_path: Path | None = None,
    include_archived: bool = False,
    finding_limit: int = 50,
    finding_status: str = "open",
    severity: str | None = None,
    lane: str | None = None,
    query: str | None = None,
) -> dict[str, Any]:
    """Load one full-spectrum audit run with filterable finding details.

    Args:
        run_id: Audit run identifier from RUN-INDEX.json.
        index_path: Optional RUN-INDEX path. Defaults to the governed
            per-user audit run index.
        include_archived: Whether archived runs can be returned.
        finding_limit: Maximum filtered findings to include.
        finding_status: Finding status filter, or ``all``.
        severity: Optional severity filter, or ``all``.
        lane: Optional lane filter, with or without the ``lane:`` prefix.
        query: Optional case-insensitive text search over finding id/title/body.

    Returns:
        A JSON-serializable run detail envelope.
    """
    safe_run_id = str(run_id).strip()
    if _RUN_ID_RE.fullmatch(safe_run_id) is None:
        return {"status": "not_found", "run_id": safe_run_id, "error": "unknown full-spectrum audit run"}

    resolved_index, index_payload, raw_runs, runs_root = _load_index(index_path)
    if not isinstance(index_payload, dict):
        return {
            "status": "unavailable",
            "run_id": safe_run_id,
            "index_path": _display_path(resolved_index),
            "error": "full-spectrum audit RUN-INDEX.json is missing or unreadable",
        }

    for raw_run in raw_runs:
        if not isinstance(raw_run, dict) or str(raw_run.get("run_id") or "") != safe_run_id:
            continue
        if raw_run.get("archived") and not include_archived:
            return {"status": "not_found", "run_id": safe_run_id, "error": "archived run hidden"}
        run_root = _safe_run_root(resolved_index, runs_root, safe_run_id, raw_run.get("run_root"))
        if run_root is None:
            return {"status": "unavailable", "run_id": safe_run_id, "error": "audit run root is missing or unsafe"}
        return {
            "status": "ok",
            "schema_version": "1.0.0",
            "index_path": _display_path(resolved_index),
            "run": _summarize_run_detail(
                raw_run,
                run_root,
                finding_limit=max(1, min(int(finding_limit), 250)),
                finding_status=finding_status,
                severity=severity,
                lane=lane,
                query=query,
            ),
        }

    return {"status": "not_found", "run_id": safe_run_id, "error": "unknown full-spectrum audit run"}


def _load_index(index_path: Path | None) -> tuple[Path, dict[str, Any] | None, list[Any], Path]:
    resolved_index = (index_path or _DEFAULT_INDEX_PATH).resolve()
    index_payload = _read_json(resolved_index)
    raw_runs = index_payload.get("runs", []) if isinstance(index_payload, dict) else []
    if not isinstance(raw_runs, list):
        raw_runs = []
    return (
        resolved_index,
        index_payload if isinstance(index_payload, dict) else None,
        raw_runs,
        resolved_index.parent / "runs",
    )


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read audit result JSON at %s: %s", path, exc)
        return None


def _safe_run_root(index_path: Path, runs_root: Path, run_id: str, raw_run_root: object) -> Path | None:
    if not run_id or _RUN_ID_RE.fullmatch(run_id) is None:
        return None
    candidates: list[Path] = []
    if isinstance(raw_run_root, str) and raw_run_root.strip():
        raw = Path(raw_run_root)
        if raw.is_absolute():
            candidates.append(raw)
        else:
            candidates.extend([(_ROOT / raw).resolve(), (index_path.parent / raw).resolve()])
    candidates.append((runs_root / run_id).resolve())
    allowed = runs_root.resolve()
    for candidate in candidates:
        resolved = candidate.resolve()
        if _is_relative_to(resolved, allowed) and resolved.is_dir():
            return resolved
    return None


def _summarize_run(raw_run: dict[str, Any], run_root: Path) -> dict[str, Any]:
    registry = _read_json(run_root / "finding-registry.json")
    closure = _read_json(run_root / "CLOSURE-STATUS.json")
    checkpoint = _read_json(run_root / "CHECKPOINT-STATE.json")
    findings = registry.get("findings", []) if isinstance(registry, dict) else []
    closure_rows = closure.get("findings", []) if isinstance(closure, dict) else []
    open_findings = [row for row in findings if isinstance(row, dict) and str(row.get("status", "")).lower() == "open"]
    return {
        "run_id": str(raw_run.get("run_id") or run_root.name),
        "status": str(raw_run.get("status") or _checkpoint_value(checkpoint, "phase", "unknown")),
        "phase": _checkpoint_value(checkpoint, "phase", None),
        "current_round": _checkpoint_value(checkpoint, "current_round", None),
        "started_at": raw_run.get("started_at"),
        "completed_at": raw_run.get("completed_at"),
        "scope_note": raw_run.get("scope_note"),
        "head_commit": raw_run.get("head_commit"),
        "archived": bool(raw_run.get("archived")),
        "pinned": bool(raw_run.get("pinned")),
        "run_flags": _run_flags(raw_run),
        "lanes_completed": raw_run.get("lanes_completed", []),
        "correction_phase": raw_run.get("correction_phase"),
        "correction_phase_note": raw_run.get("correction_phase_note"),
        "run_root": _display_path(run_root),
        "finding_count": len(findings) if isinstance(findings, list) else 0,
        "open_findings": len(open_findings),
        "severity_counts": _count_field(findings, "severity"),
        "status_counts": _count_field(findings, "status"),
        "closure_status_counts": _count_field(closure_rows, "closure_status"),
        "lane_counts": _count_lanes(findings),
        "artifact_refs": _present_artifacts(run_root),
        "top_findings": _top_findings(open_findings),
    }


def _summarize_run_detail(
    raw_run: dict[str, Any],
    run_root: Path,
    *,
    finding_limit: int,
    finding_status: str,
    severity: str | None,
    lane: str | None,
    query: str | None,
) -> dict[str, Any]:
    summary = _summarize_run(raw_run, run_root)
    registry = _read_json(run_root / "finding-registry.json")
    closure = _read_json(run_root / "CLOSURE-STATUS.json")
    findings = registry.get("findings", []) if isinstance(registry, dict) else []
    closure_rows = closure.get("findings", []) if isinstance(closure, dict) else []
    closure_by_id = _closure_rows_by_id(closure_rows)
    filtered = [
        row
        for row in findings
        if isinstance(row, dict)
        and _matches_finding_filters(
            row,
            finding_status=finding_status,
            severity=severity,
            lane=lane,
            query=query,
        )
    ]
    filtered.sort(
        key=lambda row: (_SEVERITY_RANK.get(str(row.get("severity") or "").lower(), 9), str(row.get("id") or ""))
    )
    summary.update({
        "finding_filter": {
            "status": _normalize_filter(finding_status, default="open"),
            "severity": _normalize_filter(severity, default="all"),
            "lane": _normalize_lane_filter(lane),
            "query": str(query or "").strip(),
        },
        "finding_result_count": len(filtered),
        "finding_limit": finding_limit,
        "findings": [_serialize_finding(row, closure_by_id) for row in filtered[:finding_limit]],
        "lane_artifacts": _lane_artifacts(run_root),
    })
    return summary


def _checkpoint_value(payload: Any, key: str, default: Any) -> Any:
    return payload.get(key, default) if isinstance(payload, dict) else default


def _count_field(rows: Any, key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    if not isinstance(rows, list):
        return counts
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = str(row.get(key) or "unknown").strip().lower() or "unknown"
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _count_lanes(rows: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    if not isinstance(rows, list):
        return counts
    for row in rows:
        if not isinstance(row, dict):
            continue
        lane = str(row.get("scope") or "").removeprefix("lane:") or "unknown"
        counts[lane] = counts.get(lane, 0) + 1
    return dict(sorted(counts.items()))


def _run_flags(raw_run: dict[str, Any]) -> dict[str, bool]:
    names = (
        "has_handoff_brief",
        "has_prevention_handoff",
        "has_contradiction_report",
        "has_lessons_applied",
    )
    return {name: bool(raw_run.get(name)) for name in names if name in raw_run}


def _closure_rows_by_id(rows: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(rows, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        finding_id = str(row.get("finding_id") or row.get("id") or "").strip()
        if finding_id:
            result[finding_id] = row
    return result


def _matches_finding_filters(
    row: dict[str, Any],
    *,
    finding_status: str,
    severity: str | None,
    lane: str | None,
    query: str | None,
) -> bool:
    status_filter = _normalize_filter(finding_status, default="open")
    if status_filter != "all" and str(row.get("status") or "").strip().lower() != status_filter:
        return False
    severity_filter = _normalize_filter(severity, default="all")
    if severity_filter != "all" and str(row.get("severity") or "").strip().lower() != severity_filter:
        return False
    lane_filter = _normalize_lane_filter(lane)
    row_lane = str(row.get("scope") or "").lower().removeprefix("lane:") or "unknown"
    if lane_filter != "all" and row_lane != lane_filter:
        return False
    query_text = str(query or "").strip().lower()
    if not query_text:
        return True
    haystack = " ".join(
        str(row.get(key) or "")
        for key in ("id", "finding_id", "title", "root_cause", "impact", "broken", "missing", "scope")
    ).lower()
    return query_text in haystack


def _normalize_filter(value: str | None, *, default: str) -> str:
    normalized = str(value or default).strip().lower()
    return normalized or default


def _normalize_lane_filter(value: str | None) -> str:
    normalized = str(value or "all").strip().lower().removeprefix("lane:")
    return normalized or "all"


def _serialize_finding(row: dict[str, Any], closure_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    finding_id = str(row.get("id") or row.get("finding_id") or "")
    closure = closure_by_id.get(finding_id, {})
    return {
        "id": finding_id,
        "title": str(row.get("title") or ""),
        "status": str(row.get("status") or "unknown"),
        "severity": str(row.get("severity") or "unknown"),
        "lane": str(row.get("scope") or "").removeprefix("lane:") or "unknown",
        "confidence": row.get("confidence"),
        "evidence_tier": row.get("evidence_tier"),
        "root_cause": row.get("root_cause"),
        "impact": row.get("impact"),
        "updated_at": row.get("updated_at"),
        "artifacts": row.get("artifacts", []),
        "closure_status": closure.get("closure_status"),
        "blocker_status": closure.get("blocker_status"),
        "changed_paths": closure.get("changed_paths", []),
    }


def _lane_artifacts(run_root: Path) -> list[dict[str, str]]:
    artifacts: list[dict[str, str]] = []
    for lane_dir in sorted(path for path in run_root.iterdir() if path.is_dir() and not path.name.startswith(".")):
        for name in ("FINDINGS.md", "LANE-EVIDENCE.json", "ROUND2-BROWSER-PROOF.json"):
            candidate = lane_dir / name
            if candidate.exists():
                artifacts.append({"lane": lane_dir.name, "path": _display_path(candidate)})
    return artifacts


def _present_artifacts(run_root: Path) -> list[str]:
    names = ("finding-registry.json", "CLOSURE-STATUS.json", "CHECKPOINT-STATE.json", "HANDOFF-BRIEF.md")
    return [_display_path(run_root / name) for name in names if (run_root / name).exists()]


def _top_findings(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    sorted_rows = sorted(
        rows,
        key=lambda row: (_SEVERITY_RANK.get(str(row.get("severity") or "").lower(), 9), str(row.get("id") or "")),
    )
    return [
        {
            "id": str(row.get("id") or row.get("finding_id") or ""),
            "title": str(row.get("title") or ""),
            "severity": str(row.get("severity") or "unknown"),
            "lane": str(row.get("scope") or "").removeprefix("lane:") or "unknown",
        }
        for row in sorted_rows[:5]
    ]


def _display_path(path: Path) -> str:
    resolved = path.resolve()
    if resolved.is_relative_to(_ROOT):
        return resolved.relative_to(_ROOT).as_posix()
    return resolved.as_posix()


def _is_relative_to(path: Path, parent: Path) -> bool:
    return path.is_relative_to(parent)
