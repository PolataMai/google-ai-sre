"""评测(F04/F07 验收口径):对回放结果按 Gold 计算召回率与精确匹配率。

统一口径(与指标表一致,可从记录重算):
- Top-3 召回率 = Gold 根因进入前三候选的案例数 ÷ 有 Gold 根因的案例数;
- L2 精确匹配率 = 动作类型+目标+标准化参数全匹配 ÷ 有 Gold 动作的案例数;
- 参数标准化:字典深度相等,与键序无关;
- 无 Gold 的案例不进分母(不能拿未标注案例灌水)。
"""
import unittest

from aisre.evaluation import evaluate_replays, exact_match
from aisre.replay import ReplayCase, replay_case
from tests.test_replay import CASE


def variant(case_id, snapshots=None, gold="keep"):
    d = dict(CASE, case_id=case_id)
    if snapshots is not None:
        d["snapshots"] = snapshots
    if gold != "keep":
        if gold is None:
            d.pop("gold", None)
        else:
            d["gold"] = gold
    return d


CAPACITY_SNAPSHOTS = {
    "metrics": {"conn_pool_used_pct": 97.0, "current_replicas": 20},
    "logs": {"error_lines": 5},
    "release": {"version": "v41"},
    "topology": {"downstream": ["order-db"]},
}


class TestExactMatch(unittest.TestCase):
    def _plan(self):
        return replay_case(ReplayCase.from_dict(CASE)).plan

    def test_match_independent_of_key_order(self):
        gold = {"action_type": "rollback_release",
                "target": {"workload": "payment-api", "cluster": "prod-cn-east",
                           "namespace": "payment"},
                "parameters": {"rollback_to_version": "v41",
                               "current_version": "v42"}}
        self.assertTrue(exact_match(self._plan(), gold))

    def test_parameter_difference_fails(self):
        gold = dict(CASE["gold"]["action"],
                    parameters={"current_version": "v42",
                                "rollback_to_version": "v40"})
        self.assertFalse(exact_match(self._plan(), gold))

    def test_none_plan_never_matches(self):
        self.assertFalse(exact_match(None, CASE["gold"]["action"]))


class TestEvaluateReplays(unittest.TestCase):
    def test_full_report(self):
        cases = [
            # 命中:Top-1 即 Gold 根因,动作精确匹配
            ReplayCase.from_dict(variant("case-hit")),
            # 召回命中但动作不匹配(Gold 是人工扩到 26,计划器 23)
            ReplayCase.from_dict(variant(
                "case-plan-miss", snapshots=CAPACITY_SNAPSHOTS,
                gold={"cause_code": "CAPACITY_SATURATION",
                      "action": {"action_type": "scale_out",
                                 "target": CASE["target"],
                                 "parameters": {"original_replicas": 20,
                                                "target_replicas": 26}}})),
            # 召回未命中:Gold 说是容量,回放数据只支持发布回归
            ReplayCase.from_dict(variant(
                "case-recall-miss",
                gold={"cause_code": "CAPACITY_SATURATION", "action": None})),
            # 无 Gold:不进任何分母
            ReplayCase.from_dict(variant("case-unlabeled", gold=None)),
        ]
        report = evaluate_replays([replay_case(c) for c in cases])

        self.assertEqual(report.total_cases, 4)
        self.assertEqual(report.recall_supported, 3)
        # Top-3 永远含全部三场景 → 召回 3/3(容量即使低置信也在前三)
        self.assertEqual(report.top3_hits, 3)
        self.assertAlmostEqual(report.top3_recall, 1.0)
        # Top-1 命中更严格:case-recall-miss 的 Top-1 是发布回归 ≠ Gold
        self.assertEqual(report.top1_hits, 2)
        self.assertAlmostEqual(report.top1_accuracy, 2 / 3)
        # L2 精确匹配:2 个有 Gold 动作的案例,1 个全匹配
        self.assertEqual(report.action_cases, 2)
        self.assertEqual(report.exact_matches, 1)
        self.assertAlmostEqual(report.exact_match_rate, 0.5)
        # 明细可追溯到案例
        detail = {d["case_id"]: d for d in report.details}
        self.assertTrue(detail["case-hit"]["top1_hit"])
        self.assertFalse(detail["case-plan-miss"]["exact_match"])

    def test_empty_report(self):
        report = evaluate_replays([])
        self.assertEqual(report.total_cases, 0)
        self.assertIsNone(report.top3_recall)
        self.assertIsNone(report.exact_match_rate)

    def test_report_serializable(self):
        import json
        report = evaluate_replays(
            [replay_case(ReplayCase.from_dict(CASE))])
        parsed = json.loads(json.dumps(report.to_dict(), ensure_ascii=False))
        self.assertEqual(parsed["top3_recall"], 1.0)
