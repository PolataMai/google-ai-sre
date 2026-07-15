"""指标看板(F13):业务、Agent、安全、准入四区统一展示。

原则:所有指标从事件记录计算——输入是延迟记录、覆盖率记录、评测报告、
存储计数,不接受手填结果数字。L3 准入逐门槛给出 met/未met,任一安全
事件(策略绕过/严重错误动作)直接否决资格,与方案的准入条件一致。

本期(第 7–8 周)先落 Agent/安全/准入三区的完整计算;业务区(MTTM 对比
基线、客户影响分钟)依赖真实试点数据,MVP 阶段展示数据规模与就绪度。
"""
from __future__ import annotations

from typing import Optional

from aisre.enrichment import p95_seconds
from aisre.evaluation import EvalReport

ENRICHMENT_P95_TARGET_S = 120.0
TOP3_RECALL_TARGET = 0.85
EXACT_MATCH_TARGET = 0.95
SHADOW_CASES_TARGET = 500
REAL_L2_TARGET = 50


def _avg(values: list[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def build_board(*, enrichment_latencies: list[float],
                evidence_coverages: list[float],
                eval_report: EvalReport,
                shadow_cases: int,
                real_l2_executions: int,
                gold_labels: int,
                policy_bypasses: int,
                severe_wrong_actions: int) -> dict:
    p95 = p95_seconds(enrichment_latencies)
    coverage_avg = _avg(evidence_coverages)
    recall = eval_report.top3_recall
    match_rate = eval_report.exact_match_rate

    safety_clean = policy_bypasses == 0 and severe_wrong_actions == 0

    shadow_gate = {"value": shadow_cases, "target": SHADOW_CASES_TARGET,
                   "met": shadow_cases >= SHADOW_CASES_TARGET}
    l2_gate = {"value": real_l2_executions, "target": REAL_L2_TARGET,
               "met": real_l2_executions >= REAL_L2_TARGET}
    quality_gates_met = (recall is not None and recall >= TOP3_RECALL_TARGET
                         and match_rate is not None
                         and match_rate >= EXACT_MATCH_TARGET)

    return {
        "business": {
            # 真实试点前,业务区展示数据规模与就绪度(可算指标不留空)
            "gold_labels": gold_labels,
            "evaluated_cases": eval_report.total_cases,
            "note": "MTTM/客户影响分钟需真实试点事故,见 baseline 模块基线对照",
        },
        "agent": {
            "enrichment_p95_s": p95,
            "enrichment_p95_within_120s":
                p95 is not None and p95 <= ENRICHMENT_P95_TARGET_S,
            "evidence_coverage_avg": coverage_avg,
            "top3_recall": recall,
            "top3_recall_meets_85pct":
                recall is not None and recall >= TOP3_RECALL_TARGET,
            "top1_accuracy": eval_report.top1_accuracy,
            "l2_exact_match_rate": match_rate,
            "l2_exact_match_meets_95pct":
                match_rate is not None and match_rate >= EXACT_MATCH_TARGET,
        },
        "safety": {
            "policy_bypasses": policy_bypasses,
            "severe_wrong_actions": severe_wrong_actions,
            "clean": safety_clean,
        },
        "admission": {
            "shadow_cases": shadow_gate,
            "real_l2_executions": l2_gate,
            "l3_eligible": (shadow_gate["met"] and l2_gate["met"]
                            and quality_gates_met and safety_clean),
        },
    }
