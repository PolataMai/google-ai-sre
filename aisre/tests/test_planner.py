"""Shadow 计划器:把 Top-1 假设翻译成类型化 ActionPlan 草案(只生成不执行)。

约束:
- 只在 Top-1 置信度 ≥ 0.8 且场景有白名单动作时生成;
- rollback_release 参数取自发布事实的 meta(version/previous);
- scale_out 副本数取自 metrics 快照 current_replicas,+15% 落在 10–25% 边界内;
- 生成的草案必须通过 validate_action_plan(含场景白名单校验);
- 生成失败给出机器可读原因,不猜参数。
"""
import tempfile
import unittest

from aisre.actions import validate_action_plan
from aisre.connectors import default_connectors
from aisre.enrichment import run_enrichment
from aisre.evidence_store import EvidenceStore
from aisre.intake import Alert
from aisre.planner import draft_plan
from aisre.scenarios import get_scenario

WINDOW = ("2026-07-15T10:00:00Z", "2026-07-15T10:15:00Z")
TARGET = {"cluster": "prod-cn-east", "namespace": "payment",
          "workload": "payment-api"}
EXPIRES = "2026-07-15T10:20:00Z"
NOW = "2026-07-15T10:12:00Z"

ALERT = Alert(source="alertmanager", fingerprint="abc123",
              service="payment-api", severity="critical",
              title="HighErrorRate", starts_at="2026-07-15T10:08:00Z")


def ok_client(payload):
    def fetch(service, time_range):
        return {"url": f"https://src/q?svc={service}", "query": "q",
                "snapshot": payload}
    return fetch


def run_with(metrics, release):
    tmp = tempfile.TemporaryDirectory()
    run = run_enrichment(
        incident_id="inc-001", alert=ALERT, time_range=WINDOW,
        connectors=default_connectors(
            metrics=ok_client(metrics),
            logs=ok_client({"error_lines": 240}),
            trace=ok_client({"error_spans": 3}),
            release=ok_client(release),
            topology=ok_client({"downstream": ["db"]})),
        store=EvidenceStore(tmp.name), published_at="2026-07-15T10:09:20Z")
    return run, tmp


class TestRollbackPlanning(unittest.TestCase):
    def setUp(self):
        self.run, tmp = run_with(
            metrics={"error_rate_before": 0.002, "error_rate_after": 0.081},
            release={"version": "v42", "previous": "v41",
                     "deployed_at": "2026-07-15T10:05:00Z"})
        self.addCleanup(tmp.cleanup)

    def test_drafts_valid_rollback_plan(self):
        plan, reason = draft_plan(self.run, target=TARGET, expires_at=EXPIRES)
        self.assertIsNone(reason)
        self.assertEqual(plan.action_type, "rollback_release")
        self.assertEqual(plan.parameters,
                         {"current_version": "v42",
                          "rollback_to_version": "v41"})
        self.assertEqual(plan.rollback,
                         {"action_type": "redeploy_version", "version": "v42"})
        scenario = get_scenario("RECENT_RELEASE_REGRESSION")
        self.assertEqual(
            validate_action_plan(plan, now=NOW, scenario=scenario), [])

    def test_plan_is_deterministic(self):
        a, _ = draft_plan(self.run, target=TARGET, expires_at=EXPIRES)
        b, _ = draft_plan(self.run, target=TARGET, expires_at=EXPIRES)
        self.assertEqual(a.plan_hash(), b.plan_hash())


class TestScaleOutPlanning(unittest.TestCase):
    def test_drafts_valid_scale_out_plan(self):
        run, tmp = run_with(
            metrics={"conn_pool_used_pct": 97.0, "current_replicas": 20},
            release={"version": "v41"})
        self.addCleanup(tmp.cleanup)
        plan, reason = draft_plan(run, target=TARGET, expires_at=EXPIRES)
        self.assertIsNone(reason)
        self.assertEqual(plan.action_type, "scale_out")
        self.assertEqual(plan.parameters,
                         {"original_replicas": 20, "target_replicas": 23})
        self.assertEqual(plan.rollback["target_replicas"], 20)
        scenario = get_scenario("CAPACITY_SATURATION")
        self.assertEqual(
            validate_action_plan(plan, now=NOW, scenario=scenario), [])

    def test_missing_replicas_refuses_to_guess(self):
        run, tmp = run_with(metrics={"conn_pool_used_pct": 97.0},
                            release={"version": "v41"})
        self.addCleanup(tmp.cleanup)
        plan, reason = draft_plan(run, target=TARGET, expires_at=EXPIRES)
        self.assertIsNone(plan)
        self.assertEqual(reason, "missing_current_replicas")


class TestRefusals(unittest.TestCase):
    def test_low_confidence_refuses(self):
        run, tmp = run_with(metrics={"error_rate_before": 0.002,
                                     "error_rate_after": 0.081},
                            release={"version": "v41"})   # 无窗口内发布
        self.addCleanup(tmp.cleanup)
        plan, reason = draft_plan(run, target=TARGET, expires_at=EXPIRES)
        self.assertIsNone(plan)
        self.assertEqual(reason, "low_confidence")

    def test_investigate_only_scenario_refuses(self):
        run, tmp = run_with(
            metrics={"instance_error_rates": {"pod-1": 0.001, "pod-2": 0.002,
                                              "pod-3": 0.4}},
            release={"version": "v41"})
        self.addCleanup(tmp.cleanup)
        plan, reason = draft_plan(run, target=TARGET, expires_at=EXPIRES)
        self.assertIsNone(plan)
        self.assertEqual(reason, "investigate_only")
