"""评测:对回放结果按 Gold 标注计算验收指标(F04/F07 口径)。

- Top-3 召回率 = Gold 根因进入前三候选 ÷ 有 Gold 根因的案例(目标 ≥85%);
- Top-1 准确率单独报告(Top-3 永远含全部三场景时,召回率会偏乐观,
  Top-1 才反映排序质量——两个口径都给,避免自欺);
- L2 精确匹配 = 动作类型 + 目标 + 标准化参数全等 ÷ 有 Gold 动作的案例
  (目标 ≥95%);模糊接近不算对——对应文章的确定性打分标准;
- 无 Gold 的案例不进任何分母。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from aisre.actions import ActionPlan
from aisre.replay import ReplayResult


def _canonical(d: dict) -> str:
    return json.dumps(d, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


def exact_match(plan: Optional[ActionPlan], gold_action: dict) -> bool:
    """动作类型、目标、标准化参数全匹配才算对(与键序无关)。"""
    if plan is None:
        return False
    return (plan.action_type == gold_action["action_type"]
            and _canonical(plan.target) == _canonical(gold_action["target"])
            and _canonical(plan.parameters)
            == _canonical(gold_action["parameters"]))


@dataclass
class EvalReport:
    total_cases: int
    recall_supported: int          # 有 Gold 根因的案例数(召回率分母)
    top3_hits: int
    top1_hits: int
    action_cases: int              # 有 Gold 动作的案例数(匹配率分母)
    exact_matches: int
    details: list[dict] = field(default_factory=list)

    @property
    def top3_recall(self) -> Optional[float]:
        return (self.top3_hits / self.recall_supported
                if self.recall_supported else None)

    @property
    def top1_accuracy(self) -> Optional[float]:
        return (self.top1_hits / self.recall_supported
                if self.recall_supported else None)

    @property
    def exact_match_rate(self) -> Optional[float]:
        return (self.exact_matches / self.action_cases
                if self.action_cases else None)

    def to_dict(self) -> dict:
        return {
            "total_cases": self.total_cases,
            "recall_supported": self.recall_supported,
            "top3_hits": self.top3_hits,
            "top3_recall": self.top3_recall,
            "top1_hits": self.top1_hits,
            "top1_accuracy": self.top1_accuracy,
            "action_cases": self.action_cases,
            "exact_matches": self.exact_matches,
            "exact_match_rate": self.exact_match_rate,
            "details": self.details,
        }


def evaluate_replays(results: list[ReplayResult]) -> EvalReport:
    report = EvalReport(total_cases=len(results), recall_supported=0,
                        top3_hits=0, top1_hits=0, action_cases=0,
                        exact_matches=0)
    for r in results:
        detail = {"case_id": r.case_id, "top3": r.top3,
                  "plan_refusal": r.plan_refusal,
                  "top3_hit": None, "top1_hit": None, "exact_match": None}
        gold = r.gold or {}
        gold_cause = gold.get("cause_code")
        if gold_cause:
            report.recall_supported += 1
            detail["top3_hit"] = gold_cause in r.top3
            detail["top1_hit"] = bool(r.top3) and r.top3[0] == gold_cause
            report.top3_hits += detail["top3_hit"]
            report.top1_hits += detail["top1_hit"]
        gold_action = gold.get("action")
        if gold_action:
            report.action_cases += 1
            detail["exact_match"] = exact_match(r.plan, gold_action)
            report.exact_matches += detail["exact_match"]
        report.details.append(detail)
    return report
