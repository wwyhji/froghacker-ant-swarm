"""Quality verification — cross-Claude finding verification and conflict resolution."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Verdict:
    """Verification result for a finding."""
    finding_id: str
    status: str  # verified | disputed | failed | inconclusive
    confidence: str  # high | medium | low
    verified_by: str = ""
    note: str = ""
    conflicting_findings: list[str] = field(default_factory=list)


def verify_finding(finding: dict) -> Verdict:
    """Auto-verify a finding based on completeness criteria.

    Returns a Verdict without cross-Claude check (used when only one Worker).
    """
    issues = []
    has_summary = bool(finding.get("summary"))
    has_detail = bool(finding.get("detail"))
    has_evidence = bool(finding.get("evidence_path") or finding.get("evidence"))

    if not has_summary:
        issues.append("缺少摘要")
    if not has_detail:
        issues.append("缺少详细描述")
    if not has_evidence:
        issues.append("缺少证据路径")

    if not issues and has_detail and has_evidence:
        return Verdict(
            finding_id=finding.get("id", "?"),
            status="verified",
            confidence="high" if has_evidence else "medium",
            note="自动验证通过"
        )
    if issues:
        return Verdict(
            finding_id=finding.get("id", "?"),
            status="inconclusive",
            confidence="low",
            note="; ".join(issues)
        )
    return Verdict(
        finding_id=finding.get("id", "?"),
        status="inconclusive",
        confidence="low",
        note="信息不足"
    )


def cross_verify(findings_a: list[dict], findings_b: list[dict]) -> list[Verdict]:
    """Cross-verify findings from two Workers.

    Matches findings by summary similarity, then checks for conflicts.
    """
    verdicts = []
    a_by_id = {f.get("id", ""): f for f in findings_a if f.get("id")}
    b_by_id = {f.get("id", ""): f for f in findings_b if f.get("id")}

    all_ids = set(a_by_id) | set(b_by_id)
    for fid in all_ids:
        f_a = a_by_id.get(fid)
        f_b = b_by_id.get(fid)

        if f_a and f_b:
            # Both Workers reported this finding — check agreement
            a_sum = (f_a.get("summary") or "").strip().lower()
            b_sum = (f_b.get("summary") or "").strip().lower()
            if a_sum == b_sum:
                verdicts.append(Verdict(
                    finding_id=fid, status="verified",
                    confidence="high",
                    note="两个 Worker 一致确认"
                ))
            else:
                verdicts.append(Verdict(
                    finding_id=fid, status="disputed",
                    confidence="medium",
                    note="两个 Worker 描述不一致，需要第三个 Worker 裁定",
                    conflicting_findings=[fid],
                ))
        elif f_a:
            verdicts.append(verify_finding(f_a))
        elif f_b:
            verdicts.append(verify_finding(f_b))

    return verdicts


def check_answer_completeness(answers: list[dict]) -> list[dict]:
    """Check that all required fields are present in answers."""
    required = {"category", "qid", "answer", "confidence",
                "source_role", "evidence", "analysis", "evidence_path"}
    issues = []
    for a in answers:
        missing = required - set(a.keys())
        if missing:
            issues.append({
                "qid": a.get("qid", "?"),
                "missing": list(missing),
            })
    return issues
