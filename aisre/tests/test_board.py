"""指标看板(F13):业务、Agent、安全、准入四区统一展示。

验收:所有指标都从事件记录算出——输入是记录列表/评测报告/存储计数,
不接受手填数字;L3 准入进度按 500 例 Shadow、50 次真实 L2、零安全事件
等门槛逐项给出 met/未met。
"""
import unittest

from aisre.board import build_board
from aisre.evaluation import evaluate_replays
from aisre.replay import ReplayCase, replay_case
from tests.test_replay import CASE


def make_eval_report():
    return evaluate_replays([replay_case(ReplayCase.from_dict(CASE))])


class TestBoard(unittest.TestCase):
    def setUp(self):
        self.board = build_board(
            enrichment_latencies=[60.0, 70.0, 80.0, 90.0, 100.0],
            evidence_coverages=[1.0, 1.0, 0.8],
            eval_report=make_eval_report(),
            shadow_cases=120,
            real_l2_executions=5,
            gold_labels=40,
            policy_bypasses=0,
            severe_wrong_actions=0,
        )

    def test_agent_section_computed_from_records(self):
        agent = self.board["agent"]
        self.assertEqual(agent["enrichment_p95_s"], 100.0)   # nearest-rank
        self.assertAlmostEqual(agent["evidence_coverage_avg"], 2.8 / 3)
        self.assertEqual(agent["top3_recall"], 1.0)
        self.assertEqual(agent["l2_exact_match_rate"], 1.0)

    def test_agent_targets_flagged(self):
        agent = self.board["agent"]
        self.assertTrue(agent["enrichment_p95_within_120s"])
        self.assertTrue(agent["top3_recall_meets_85pct"])

    def test_safety_section(self):
        safety = self.board["safety"]
        self.assertEqual(safety["policy_bypasses"], 0)
        self.assertEqual(safety["severe_wrong_actions"], 0)
        self.assertTrue(safety["clean"])

    def test_admission_progress(self):
        adm = self.board["admission"]
        self.assertEqual(adm["shadow_cases"], {"value": 120, "target": 500,
                                               "met": False})
        self.assertEqual(adm["real_l2_executions"], {"value": 5, "target": 50,
                                                     "met": False})
        self.assertFalse(adm["l3_readiness_preview"])
        # board 只给就绪预览,授权门禁另在 admission 模块
        self.assertIn("authoritative_gate", adm)

    def test_readiness_preview_is_only_dev_side_not_authoritative(self):
        # 开发侧四项都满足 → 预览为 True,但这只是"就绪预览",
        # 不是 L3 授权(授权须过 admission.evaluate_l3_admission 的试点门禁)
        board = build_board(
            enrichment_latencies=[60.0],
            evidence_coverages=[1.0],
            eval_report=make_eval_report(),
            shadow_cases=520, real_l2_executions=55, gold_labels=200,
            policy_bypasses=0, severe_wrong_actions=0)
        self.assertTrue(board["admission"]["l3_readiness_preview"])
        self.assertNotIn("l3_eligible", board["admission"])  # 不冒充授权
        # 任一安全事件直接否决预览
        board2 = build_board(
            enrichment_latencies=[60.0],
            evidence_coverages=[1.0],
            eval_report=make_eval_report(),
            shadow_cases=520, real_l2_executions=55, gold_labels=200,
            policy_bypasses=0, severe_wrong_actions=1)
        self.assertFalse(board2["admission"]["l3_readiness_preview"])
        self.assertFalse(board2["safety"]["clean"])

    def test_board_serializable_and_has_business_section(self):
        import json
        parsed = json.loads(json.dumps(self.board, ensure_ascii=False))
        self.assertIn("business", parsed)
        self.assertIn("gold_labels", parsed["business"])

    def test_empty_records_yield_none_not_crash(self):
        board = build_board(enrichment_latencies=[], evidence_coverages=[],
                            eval_report=evaluate_replays([]),
                            shadow_cases=0, real_l2_executions=0,
                            gold_labels=0, policy_bypasses=0,
                            severe_wrong_actions=0)
        self.assertIsNone(board["agent"]["enrichment_p95_s"])
        self.assertIsNone(board["agent"]["top3_recall"])
        self.assertFalse(board["admission"]["l3_readiness_preview"])
