"""时间切片回放与 Shadow(F11):历史事故重放整条丰富+规划链路。

- ReplayCase 记录告警 + 当时各源的快照(时间切片)+ Gold 标注;
- 回放 = 用录制快照构造连接器,跑真实的 run_enrichment + draft_plan;
- 快照里缺的源回放为"当时不可用"(missing),与线上行为一致;
- Shadow 日志追加式落盘,案例数直接服务 500 例准入门槛;
- 同一案例回放两次结果一致(确定性,评测可复现)。
"""
import tempfile
import unittest

from aisre.replay import ReplayCase, ShadowLog, replay_case

CASE = {
    "case_id": "case-001",
    "alert": {"source": "alertmanager", "fingerprint": "abc123",
              "service": "payment-api", "severity": "critical",
              "title": "HighErrorRate", "starts_at": "2026-07-15T10:08:00Z"},
    "time_range": ["2026-07-15T10:00:00Z", "2026-07-15T10:15:00Z"],
    "target": {"cluster": "prod-cn-east", "namespace": "payment",
               "workload": "payment-api"},
    "snapshots": {
        "metrics": {"error_rate_before": 0.002, "error_rate_after": 0.081},
        "logs": {"error_lines": 240},
        "release": {"version": "v42", "previous": "v41",
                    "deployed_at": "2026-07-15T10:05:00Z"},
        "topology": {"downstream": ["order-db"]},
    },
    "gold": {"cause_code": "RECENT_RELEASE_REGRESSION",
             "action": {"action_type": "rollback_release",
                        "target": {"cluster": "prod-cn-east",
                                   "namespace": "payment",
                                   "workload": "payment-api"},
                        "parameters": {"current_version": "v42",
                                       "rollback_to_version": "v41"}}},
}


class TestReplayCase(unittest.TestCase):
    def test_roundtrip(self):
        case = ReplayCase.from_dict(CASE)
        self.assertEqual(case.to_dict(), CASE)

    def test_replay_reproduces_top1_and_plan(self):
        result = replay_case(ReplayCase.from_dict(CASE))
        self.assertEqual(result.case_id, "case-001")
        self.assertEqual(result.top3[0], "RECENT_RELEASE_REGRESSION")
        self.assertIsNotNone(result.plan)
        self.assertEqual(result.plan.action_type, "rollback_release")
        self.assertEqual(result.plan.parameters["rollback_to_version"], "v41")
        self.assertIsNone(result.plan_refusal)

    def test_absent_source_replays_as_missing(self):
        data = dict(CASE, snapshots={k: v for k, v in CASE["snapshots"].items()
                                     if k != "release"})
        result = replay_case(ReplayCase.from_dict(data))
        self.assertIn("release", result.run.missing_sources)
        self.assertIn("trace", result.run.missing_sources)   # 本就没录 trace
        self.assertIsNone(result.plan)
        self.assertEqual(result.plan_refusal, "low_confidence")

    def test_replay_is_deterministic(self):
        a = replay_case(ReplayCase.from_dict(CASE))
        b = replay_case(ReplayCase.from_dict(CASE))
        self.assertEqual(a.top3, b.top3)
        self.assertEqual(a.plan.plan_hash(), b.plan.plan_hash())


class TestShadowLog(unittest.TestCase):
    def test_append_count_and_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = ShadowLog(tmp)
            result = replay_case(ReplayCase.from_dict(CASE))
            log.record(result, at="2026-07-15T10:09:30Z")
            data = dict(CASE, case_id="case-002",
                        snapshots={"metrics": CASE["snapshots"]["metrics"]})
            log.record(replay_case(ReplayCase.from_dict(data)),
                       at="2026-07-15T10:20:00Z")
            reopened = ShadowLog(tmp)
            self.assertEqual(reopened.count(), 2)
            entries = reopened.list()
            self.assertEqual(entries[0]["plan"]["action_type"],
                             "rollback_release")
            self.assertIsNone(entries[1]["plan"])
            self.assertEqual(entries[1]["plan_refusal"], "low_confidence")
