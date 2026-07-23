"""Execution logic for Inspector agent code-review and security-audit modes.

These module-level functions accept the InspectorAgent instance so they can
call agent methods without inheriting from the class.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from vetinari.agents.contracts import AgentResult, AgentTask
from vetinari.config.inference_config import get_inference_config
from vetinari.constants import TRUNCATE_CODE_ANALYSIS, TRUNCATE_CONTEXT
from vetinari.safety.prompt_sanitizer import sanitize_worker_output

logger = logging.getLogger(__name__)
_META_REVIEW_SCORE_MAX_TOKENS = 10
_QUALITY_PASS_THRESHOLD = 0.5
_REVIEW_FALLBACK_SUMMARY = "Review unavailable"


if TYPE_CHECKING:
    from vetinari.agents.consolidated.quality_agent import InspectorAgent


def run_quality_check(*, code: str, static_backend: Any | None = None) -> dict[str, Any]:
    """Run a lightweight quality check and forward deterministic static evidence.

    Returns:
        Mapping with ``passed`` and ``static_issues`` keys.
    """
    static_result = static_backend(code) if static_backend is not None else {}
    static_issues = static_result.get("issues", []) if isinstance(static_result, dict) else []
    return {"passed": not static_issues, "static_issues": static_issues}


def _publish_quality_gate_event(task_id: str, passed: bool, score: float, issues: list[str]) -> None:
    """Publish a QualityGateResult event to the event bus.

    Logs at WARNING if the event bus is unavailable so the caller never has to
    catch this internally.

    Args:
        task_id: Identifier of the task that was quality-checked.
        passed: Whether the quality gate passed.
        score: Numeric quality score between 0.0 and 1.0.
        issues: Human-readable list of issue messages.
    """
    try:
        from vetinari.events import QualityGateResult, get_event_bus

        get_event_bus().publish(
            QualityGateResult(
                event_type="QualityGateResult",
                timestamp=time.time(),
                task_id=task_id,
                passed=passed,
                score=float(score),
                issues=issues,
            )
        )
    except Exception:
        logger.warning(
            "Could not publish QualityGateResult event for task %s - event bus unavailable",
            task_id,
        )


def execute_code_review(agent: InspectorAgent, task: AgentTask) -> AgentResult:
    """Run the code-review execution pipeline for one task.

    Args:
        agent: The InspectorAgent instance, used for review helpers.
        task: The AgentTask containing code, file_path, and review_type context keys.

    Returns:
        AgentResult with review findings and quality score.
    """
    code, review_type, target_file = _code_review_inputs(task)
    static_findings = _run_static_analysis(target_file)
    quality_check = run_quality_check(code=code, static_backend=lambda _code: {"issues": static_findings})
    antipattern_findings = agent._run_antipattern_scan(code)
    structured_context = _structured_review_context(target_file)
    result = _run_code_review_llm(agent, code, review_type, antipattern_findings, static_findings, structured_context)

    _merge_static_analysis_findings(result, static_findings)
    _merge_antipattern_findings(result, antipattern_findings)
    _add_reflexion(agent, result, review_type)
    _add_meta_review_score(result, review_type)
    rubric_score = _dimension_rubric_score(agent, result)
    result, is_fallback = _promote_antipattern_fallback(result, antipattern_findings, review_type)
    overall_score = _overall_review_score(result, review_type)

    agent_result = _build_code_review_result(
        result, review_type, antipattern_findings, static_findings, overall_score, rubric_score, is_fallback
    )
    agent_result.metadata["static_quality_check_passed"] = bool(quality_check["passed"])
    score = _review_score(result)
    score = _apply_self_check_override(task, agent_result, score)
    if score < _QUALITY_PASS_THRESHOLD:
        agent_result = agent._perform_root_cause_analysis(task, agent_result)
    _publish_review_event(task, result, score)
    return agent_result


def _code_review_inputs(task: AgentTask) -> tuple[str, str, str | None]:
    raw_code = task.context.get("code", task.description)
    code = sanitize_worker_output(raw_code) if raw_code else ""
    review_type = task.context.get("review_type", "general")
    target_file = task.context.get("file_path")
    return code, str(review_type), str(target_file) if target_file else None


def _run_static_analysis(target_file: str | None) -> list[dict[str, Any]]:
    if not target_file:
        return []
    try:
        from pathlib import Path

        from vetinari.tools.static_analysis import run_static_analysis

        sa_result = run_static_analysis(Path(target_file))
        return [
            {
                "severity": str(f.severity).lower(),
                "category": "static-analysis",
                "message": str(f.message),
                "line": int(f.line or 0),
                "suggestion": f"Reported by {f.tool} at line {f.line or 0}",
                "tool": str(f.tool),
            }
            for f in sa_result.findings[:20]
        ]
    except Exception:
        logger.warning("Static analysis unavailable for code review of %s - skipping static findings", target_file)
        return []


def _structured_review_context(target_file: str | None) -> str:
    if not target_file:
        return ""
    structured_context = _repo_map_context(target_file)
    try:
        from vetinari.grep_context import GrepContext

        test_refs = GrepContext().find_references(target_file, pattern="test_")
        if test_refs:
            structured_context += f"\n\nRelated tests:\n{test_refs[:800]}"
    except Exception:
        logger.warning(
            "Grep context unavailable for code review of %s - proceeding without test references", target_file
        )
    return structured_context


def _repo_map_context(target_file: str) -> str:
    try:
        from pathlib import Path

        from vetinari.repo_map import get_repo_map

        task_context = get_repo_map().generate_for_task(str(Path(target_file).parent), target_file)
        return f"\n\nCaller context:\n{task_context[:1500]}" if task_context else ""
    except Exception:
        logger.warning(
            "Repo map context unavailable for code review of %s - proceeding without caller context", target_file
        )
        return ""


def _antipattern_summary(antipattern_findings: list[dict[str, Any]]) -> str:
    if not antipattern_findings:
        return ""
    return "\n\nDeterministic anti-pattern scan found these issues:\n" + "\n".join(
        f"- [{f['severity']}] Line {f['line']}: {f['finding']}" for f in antipattern_findings[:10]
    )


def _run_code_review_llm(
    agent: InspectorAgent,
    code: str,
    review_type: str,
    antipattern_findings: list[dict[str, Any]],
    static_findings: list[dict[str, Any]],
    structured_context: str,
) -> object:
    prompt = (
        f"Review the following code for quality and maintainability:\n\n"
        f"```\n{code[:TRUNCATE_CONTEXT]}\n```\n\n"
        f"Review focus: {review_type}{_static_analysis_summary(static_findings)}"
        f"{_antipattern_summary(antipattern_findings)}{structured_context}\n\n"
        "Respond as JSON:\n"
        '{"score": 0.75, "summary": "...", '
        '"issues": [{"severity": "high|medium|low", "category": "...", '
        '"message": "...", "line": 0, "suggestion": "..."}], '
        '"strengths": [...], "recommendations": [...]}'
    )
    return agent._infer_json(prompt, fallback={"score": 0.5, "issues": [], "summary": _REVIEW_FALLBACK_SUMMARY})


def _static_analysis_summary(static_findings: list[dict[str, Any]]) -> str:
    if not static_findings:
        return ""
    return "\n\nStatic analysis found these issues:\n" + "\n".join(
        f"- [{f['severity']}] {f['tool']}:{f['line']}: {f['message']}" for f in static_findings[:10]
    )


def _merge_static_analysis_findings(result: object, static_findings: list[dict[str, Any]]) -> None:
    if not (isinstance(result, dict) and static_findings):
        return
    existing_issues = result.setdefault("issues", [])
    existing_keys = {
        (str(i.get("category", "")).lower(), str(i.get("message", "")).lower(), int(i.get("line", 0) or 0))
        for i in existing_issues
        if isinstance(i, dict)
    }
    for finding in static_findings:
        key = ("static-analysis", finding["message"].lower(), int(finding.get("line", 0) or 0))
        if key not in existing_keys:
            existing_issues.append(_review_issue_from_static(finding))


def _review_issue_from_static(finding: dict[str, Any]) -> dict[str, Any]:
    return {
        "severity": finding["severity"],
        "category": "static-analysis",
        "message": finding["message"],
        "line": finding["line"],
        "suggestion": finding["suggestion"],
        "tool": finding["tool"],
    }


def _merge_antipattern_findings(result: object, antipattern_findings: list[dict[str, Any]]) -> None:
    if not (isinstance(result, dict) and antipattern_findings):
        return
    existing_issues = result.setdefault("issues", [])
    existing_msgs = {i.get("message", "").lower() for i in existing_issues if isinstance(i, dict)}
    for finding in antipattern_findings:
        if finding["finding"].lower() not in existing_msgs:
            existing_issues.append(_review_issue_from_antipattern(finding))


def _review_issue_from_antipattern(finding: dict[str, Any]) -> dict[str, Any]:
    return {
        "severity": finding["severity"].lower(),
        "category": "maintainability",
        "message": finding["finding"],
        "line": finding["line"],
        "suggestion": f"Detected at line {finding['line']}: {finding['evidence']}",
    }


def _add_reflexion(agent: InspectorAgent, result: object, review_type: str) -> None:
    if not (isinstance(result, dict) and result.get("issues")):
        return
    try:
        from vetinari.llm_helpers import quick_llm_call

        issues_text = "\n".join(f"- [{i.get('severity', '?')}] {i.get('message', '')}" for i in result["issues"][:5])
        cfg = get_inference_config().get_profile("code_review")
        reflexion = quick_llm_call(
            prompt=(
                f"You just reviewed this code and found these issues:\n{issues_text}\n\n"
                "For each issue, cite the specific line or pattern that supports your finding. "
                "If any finding is uncertain, say so. Format: ISSUE: citation"
            ),
            system_prompt="You are a code reviewer verifying your own findings with evidence.",
            max_tokens=cfg.max_tokens,
        )
        if reflexion:
            result["reflexion"] = reflexion
    except Exception:
        logger.warning("Reflexion pass unavailable for %s - review returned without self-critique", review_type)


def _add_meta_review_score(result: object, review_type: str) -> None:
    if not isinstance(result, dict):
        return
    try:
        from vetinari.llm_helpers import quick_llm_call

        meta_score = quick_llm_call(
            prompt=(
                "Rate this code review on a scale of 0.0-1.0 for thoroughness, "
                f"actionability, and accuracy:\n\nReview summary: {result.get('summary', '')}\n"
                f"Issues found: {len(result.get('issues', []))}\n"
                "Respond with only a decimal number."
            ),
            system_prompt="You evaluate code review quality.",
            max_tokens=_META_REVIEW_SCORE_MAX_TOKENS,
        )
        if meta_score and meta_score.strip().replace(".", "", 1).isdigit():
            result["meta_review_score"] = max(0.0, min(1.0, float(meta_score.strip())))
    except Exception:
        logger.warning("Meta-rewarding pass unavailable - review quality score not recorded for %s", review_type)


def _dimension_rubric_score(agent: InspectorAgent, result: object) -> float | None:
    if not isinstance(result, dict):
        return None
    dimension_scores = {
        k: float(v)
        for k, v in result.items()
        if k not in {"score", "summary", "issues", "strengths", "recommendations", "reflexion", "meta_review_score"}
        and isinstance(v, (int, float))
    }
    if not dimension_scores:
        return None
    try:
        from vetinari.agents.skill_contract import compute_overall_score

        return compute_overall_score(dimension_scores, agent.agent_type.value if hasattr(agent, "agent_type") else "")
    except Exception:
        logger.warning("compute_overall_score unavailable for dimension rubric - rubric score omitted")
        return None


def _promote_antipattern_fallback(
    result: object,
    antipattern_findings: list[dict[str, Any]],
    review_type: str,
) -> tuple[object, bool]:
    is_fallback = not isinstance(result, dict) or (
        result.get("summary") == _REVIEW_FALLBACK_SUMMARY and not result.get("issues")
    )
    if is_fallback and antipattern_findings:
        logger.warning(
            "LLM review unavailable for %s - synthetic result built from %d antipattern finding(s)",
            review_type,
            len(antipattern_findings),
        )
        return _synthetic_antipattern_result(antipattern_findings), False
    if is_fallback:
        logger.warning(
            "Code review for %s produced no content - LLM returned fallback and no antipatterns were found", review_type
        )
    return result, is_fallback


def _synthetic_antipattern_result(antipattern_findings: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "score": 0.4,
        "summary": f"LLM review unavailable - {len(antipattern_findings)} antipattern(s) detected by static analysis",
        "issues": [_review_issue_from_antipattern(finding) for finding in antipattern_findings],
    }


def _overall_review_score(result: object, review_type: str) -> float:
    raw_score = _review_score(result)
    review_scores = {"review_score": float(raw_score)}
    if isinstance(result, dict) and "meta_review_score" in result:
        review_scores["meta_review_score"] = float(result["meta_review_score"])
    try:
        from vetinari.agents.skill_contract import compute_overall_score
        from vetinari.types import AgentType

        return compute_overall_score(review_scores, AgentType.INSPECTOR.value)
    except Exception:
        logger.warning("compute_overall_score unavailable for %s review - using raw score", review_type)
        return raw_score


def _review_score(result: object) -> float:
    return float(result.get("score", 0.0) if isinstance(result, dict) else 0.0)


def _build_code_review_result(
    result: object,
    review_type: str,
    antipattern_findings: list[dict[str, Any]],
    static_findings: list[dict[str, Any]],
    overall_score: float,
    rubric_score: float | None,
    is_fallback: bool,
) -> AgentResult:
    return AgentResult(
        success=not is_fallback,
        output=result,
        metadata={
            "mode": "code_review",
            "review_type": review_type,
            "antipattern_count": len(antipattern_findings),
            "static_analysis_count": len(static_findings),
            "overall_score": overall_score,
            "rubric_score": rubric_score,
            "is_fallback": is_fallback,
        },
        errors=(
            [f"Code review unavailable for {review_type} - no LLM output and no antipatterns detected"]
            if is_fallback
            else []
        ),
    )


def _apply_self_check_override(task: AgentTask, agent_result: AgentResult, score: float) -> float:
    if task.context.get("self_check_passed", True):
        return score
    self_check_issues = task.context.get("self_check_issues", [])
    agent_result.metadata["self_check_override"] = True
    agent_result.metadata["self_check_issues"] = self_check_issues
    logger.info("Self-check failed upstream - forcing RCA (issues=%d)", len(self_check_issues))
    return min(score, 0.4)


def _publish_review_event(task: AgentTask, result: object, score: float) -> None:
    issues_out = result.get("issues", []) if isinstance(result, dict) else []
    issue_msgs = [i.get("message", str(i)) if isinstance(i, dict) else str(i) for i in issues_out[:20]]
    _publish_quality_gate_event(str(task.task_id), score >= _QUALITY_PASS_THRESHOLD, float(score), issue_msgs)


def execute_security_audit(agent: InspectorAgent, task: AgentTask) -> AgentResult:
    """Run the security-audit execution pipeline for one task.

    Args:
        agent: The InspectorAgent instance, used for security helpers.
        task: The AgentTask containing code, file_path, and project_path context keys.

    Returns:
        AgentResult with security findings, overall_risk, and score.
    """
    code = _security_audit_code(task)
    target_path = task.context.get("file_path") or task.context.get("project_path")
    semgrep_findings = _run_semgrep_scan(str(target_path) if target_path else None)
    heuristic_findings = agent._run_heuristic_scan(code)
    deterministic_findings = heuristic_findings + semgrep_findings
    llm_result = _run_security_audit_llm(agent, code, deterministic_findings)
    if llm_result and isinstance(llm_result, dict):
        return _security_result_from_llm(agent, task, llm_result, heuristic_findings, semgrep_findings)
    return _security_heuristic_result(agent, task, heuristic_findings, semgrep_findings)


def _security_audit_code(task: AgentTask) -> str:
    raw_audit_code = task.context.get("code", task.description)
    return sanitize_worker_output(raw_audit_code) if raw_audit_code else ""


def _run_semgrep_scan(target_path: str | None) -> list[dict[str, Any]]:
    if not target_path:
        return []
    try:
        from pathlib import Path

        from vetinari.tools.semgrep_tool import run_semgrep

        sg_result = run_semgrep(Path(target_path))
        if sg_result.has_findings:
            return [
                {"severity": f.severity.lower(), "finding": f.message, "line": f.line, "tool": "semgrep"}
                for f in sg_result.findings
            ]
    except Exception:
        logger.warning("Semgrep unavailable for security audit of %s - skipping semgrep findings", target_path)
    return []


def _heuristic_summary(heuristic_findings: list[dict[str, Any]]) -> str:
    if not heuristic_findings:
        return ""
    return "\n\nHeuristic scan found these preliminary issues:\n" + "\n".join(
        f"- [{f['severity']}] {f['finding']}" for f in heuristic_findings[:10]
    )


def _run_security_audit_llm(
    agent: InspectorAgent,
    code: str,
    heuristic_findings: list[dict[str, Any]],
) -> object:
    prompt = (
        f"Perform a comprehensive security audit of this code:\n\n"
        f"```\n{code[:TRUNCATE_CODE_ANALYSIS]}\n```\n"
        f"{_heuristic_summary(heuristic_findings)}\n\n"
        "Analyze for: injection, broken auth, sensitive data exposure, "
        "XXE, broken access control, misconfig, XSS, insecure deserialization, "
        "vulnerable components, insufficient logging.\n\n"
        "Respond as JSON:\n"
        '{"findings": [{"severity": "CRITICAL|HIGH|MEDIUM|LOW|INFO", '
        '"finding": "...", "cwe": "CWE-79 (use real CWE IDs: 79=XSS, 89=SQLi, 22=PathTraversal, 78=OSCmd, 502=Deserialization, 798=HardcodedCreds, 327=BrokenCrypto, 306=MissingAuth)", "owasp": "A01-A10", '
        '"line": 0, "remediation": "...", "code_example": "..."}], '
        '"summary": "...", "overall_risk": "high|medium|low", '
        '"score": 0.75}'
    )
    return agent._infer_json(prompt, fallback=None)


def _security_result_from_llm(
    agent: InspectorAgent,
    task: AgentTask,
    llm_result: dict[str, Any],
    heuristic_findings: list[dict[str, Any]],
    semgrep_findings: list[dict[str, Any]],
) -> AgentResult:
    _merge_security_findings(llm_result, heuristic_findings + semgrep_findings)
    agent_result = AgentResult(
        success=True,
        output=llm_result,
        metadata={
            "mode": "security_audit",
            "heuristic_findings": len(heuristic_findings),
            "semgrep_findings": len(semgrep_findings),
        },
    )
    security_score = float(llm_result.get("score", 1.0))
    if security_score < _QUALITY_PASS_THRESHOLD:
        agent_result = agent._perform_root_cause_analysis(task, agent_result)
    _publish_security_event(task, llm_result.get("findings", []), security_score)
    return agent_result


def _merge_security_findings(llm_result: dict[str, Any], heuristic_findings: list[dict[str, Any]]) -> None:
    llm_findings = llm_result.get("findings", [])
    llm_finding_names = {finding.get("finding", "").lower() for finding in llm_findings}
    for finding in heuristic_findings:
        if finding["finding"].lower() not in llm_finding_names:
            llm_findings.append(finding)
    llm_result["findings"] = llm_findings
    llm_result.setdefault("heuristic_count", len(heuristic_findings))


def _security_heuristic_result(
    agent: InspectorAgent,
    task: AgentTask,
    heuristic_findings: list[dict[str, Any]],
    semgrep_findings: list[dict[str, Any]],
) -> AgentResult:
    findings = heuristic_findings + semgrep_findings
    heuristic_output = {
        "findings": findings,
        "summary": f"Deterministic security scan found {len(findings)} issues (LLM unavailable)",
        "overall_risk": "high"
        if any(str(f["severity"]).upper() in ("CRITICAL", "HIGH") for f in findings)
        else "medium",
        "score": max(0.0, 1.0 - len(findings) * 0.1),
    }
    agent_result = AgentResult(
        success=True,
        output=heuristic_output,
        metadata={
            "mode": "security_audit",
            "heuristic_only": True,
            "heuristic_findings": len(heuristic_findings),
            "semgrep_findings": len(semgrep_findings),
        },
    )
    heuristic_score = float(heuristic_output["score"])
    if heuristic_score < _QUALITY_PASS_THRESHOLD:
        agent_result = agent._perform_root_cause_analysis(task, agent_result)
    _publish_security_event(task, findings, heuristic_score)
    return agent_result


def _publish_security_event(task: AgentTask, findings: list[object], score: float) -> None:
    issue_msgs = [
        finding.get("finding", str(finding)) if isinstance(finding, dict) else str(finding) for finding in findings[:20]
    ]
    _publish_quality_gate_event(str(task.task_id), score >= _QUALITY_PASS_THRESHOLD, float(score), issue_msgs)
