"""生产 Shadow(F11):线上对真实告警只生成计划、绝不执行。

与 L2/L3 的区别不在能不能生成计划,而在生成后是否提交执行:
- shadow_evaluate 只调 planner.draft_plan,产出标记 mode="shadow" 的记录,
  不接触 gateway、不接触任何执行器——"不执行"是结构性的,不靠自觉;
- ShadowLedger 追加式累积记录,计数直接服务 L3 准入的 500 例门槛。
"""
import tempfile
import unittest

from aisre.connectors import default_connectors
from aisre.enrichment import run_enrichment
from aisre.evidence_store import EvidenceStore
from aisre.intake import Alert
from aisre.shadow import ShadowLedger, ShadowRecord, shadow_evaluate

WINDOW = ("2026-07-15T10:00:00Z", "2026-07-15T10:15:00Z")
TARGET = {"cluster": "prod-cn-east", "namespace": "payment",
          "workload": "payment-api"}
EXPIRES = "2026-07-15T10:20:00Z"

ALERT = Alert(source="alertmanager", fingerprint="abc123",
              service="payment-api", severity="critical",
              title="HighErrorRate", starts_at="2026-07-15T10:08:00Z")


def ok_client(payload):
    def fetch(service, time_range):
        return {"url": f"https://src/q?svc={service}", "query": "q",
                "snapshot": payload}
    return fetch


def make_run(metrics, release):
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


class TestShadowEvaluate(unittest.TestCase):
    def test_generates_plan_but_marks_shadow(self):
        run, tmp = make_run(
            metrics={"error_rate_before": 0.002, "error_rate_after": 0.081},
            release={"version": "v42", "previous": "v41",
                     "deployed_at": "2026-07-15T10:05:00Z"})
        self.addCleanup(tmp.cleanup)
        rec = shadow_evaluate(run, target=TARGET, expires_at=EXPIRES)
        self.assertIsInstance(rec, ShadowRecord)
        self.assertEqual(rec.mode, "shadow")
        self.assertEqual(rec.incident_id, "inc-001")
        self.assertEqual(rec.top3[0], "RECENT_RELEASE_REGRESSION")
        self.assertEqual(rec.plan["action_type"], "rollback_release")
        self.assertIsNone(rec.plan_refusal)

    def test_low_confidence_records_refusal_not_plan(self):
        run, tmp = make_run(
            metrics={"error_rate_before": 0.002, "error_rate_after": 0.081},
            release={"version": "v41"})   # 无窗口内发布 → 低置信
        self.addCleanup(tmp.cleanup)
        rec = shadow_evaluate(run, target=TARGET, expires_at=EXPIRES)
        self.assertIsNone(rec.plan)
        self.assertEqual(rec.plan_refusal, "low_confidence")

    def test_shadow_module_does_not_import_gateway(self):
        # 结构性保证"不执行":shadow 的 import 里不能出现执行侧的 gateway
        # (只看 import 行,不禁止文档里提及 gateway 说明设计意图)
        import inspect
        import aisre.shadow as shadow_mod
        import_lines = [l for l in inspect.getsource(shadow_mod).splitlines()
                        if l.startswith(("import ", "from "))]
        self.assertFalse(any("gateway" in l for l in import_lines),
                         f"shadow 不应 import gateway: {import_lines}")


class TestShadowLedger(unittest.TestCase):
    def test_append_count_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = ShadowLedger(tmp)
            run, rt = make_run(
                metrics={"error_rate_before": 0.002,
                         "error_rate_after": 0.081},
                release={"version": "v42", "previous": "v41",
                         "deployed_at": "2026-07-15T10:05:00Z"})
            self.addCleanup(rt.cleanup)
            ledger.append(shadow_evaluate(run, target=TARGET,
                                          expires_at=EXPIRES))
            reopened = ShadowLedger(tmp)
            self.assertEqual(reopened.count(), 1)
            self.assertEqual(reopened.list()[0]["plan"]["action_type"],
                             "rollback_release")
            self.assertEqual(reopened.list()[0]["mode"], "shadow")
