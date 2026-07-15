"""事故工作台(F05):首轮调查不需要跳出的单一视图。

验收对应:
- 时间线把告警、发布、事实观测按时刻排序对齐;
- 数据源状态(含缺失源)、事实(带证据链接)、Top-3 假设、建议动作齐备;
- 建议动作来自 Top-1 假设所属场景的白名单;调查型场景明确提示无自动动作;
- Markdown 渲染包含全部章节(供事故平台/IM 直接贴出)。
"""
import tempfile
import unittest

from aisre.connectors import default_connectors
from aisre.enrichment import run_enrichment
from aisre.evidence_store import EvidenceStore
from aisre.intake import Alert
from aisre.workbench import build_workbench, render_markdown

WINDOW = ("2026-07-15T10:00:00Z", "2026-07-15T10:15:00Z")

ALERT = Alert(source="alertmanager", fingerprint="abc123",
              service="payment-api", severity="critical",
              title="HighErrorRate", starts_at="2026-07-15T10:08:00Z")


def ok_client(payload):
    def fetch(service, time_range):
        return {"url": f"https://src/q?svc={service}", "query": "q",
                "snapshot": payload}
    return fetch


def broken(service, time_range):
    raise ConnectionError("不可用")


def make_run(**overrides):
    clients = {
        "metrics": ok_client({"error_rate_before": 0.002,
                              "error_rate_after": 0.081}),
        "logs": ok_client({"error_lines": 240}),
        "trace": ok_client({"error_spans": 37}),
        "release": ok_client({"version": "v42", "previous": "v41",
                              "deployed_at": "2026-07-15T10:05:00Z"}),
        "topology": ok_client({"downstream": ["order-db"]}),
    }
    clients.update(overrides)
    tmp = tempfile.TemporaryDirectory()
    run = run_enrichment(
        incident_id="inc-001", alert=ALERT, time_range=WINDOW,
        connectors=default_connectors(**clients),
        store=EvidenceStore(tmp.name), published_at="2026-07-15T10:09:20Z")
    return run, tmp


class TestBuildWorkbench(unittest.TestCase):
    def setUp(self):
        self.run, self.tmp = make_run()
        self.addCleanup(self.tmp.cleanup)
        self.wb = build_workbench(self.run, alert=ALERT)

    def test_header(self):
        self.assertEqual(self.wb["incident"]["incident_id"], "inc-001")
        self.assertEqual(self.wb["incident"]["service"], "payment-api")
        self.assertEqual(self.wb["incident"]["severity"], "critical")

    def test_timeline_sorted_and_aligned(self):
        events = self.wb["timeline"]
        times = [e["at"] for e in events]
        self.assertEqual(times, sorted(times))
        kinds = [e["kind"] for e in events]
        self.assertIn("deploy", kinds)          # 发布事件上时间线
        self.assertIn("alert", kinds)           # 告警接入上时间线
        deploy = next(e for e in events if e["kind"] == "deploy")
        alert = next(e for e in events if e["kind"] == "alert")
        self.assertLess(deploy["at"], alert["at"])   # 10:05 发布早于 10:08 告警

    def test_data_sources_include_missing(self):
        run, tmp = make_run(logs=broken)
        self.addCleanup(tmp.cleanup)
        wb = build_workbench(run, alert=ALERT)
        status = {s["source"]: s["status"] for s in wb["data_sources"]}
        self.assertEqual(status["logs"], "failed")
        self.assertEqual(status["metrics"], "ok")

    def test_facts_carry_evidence_links(self):
        for fact in self.wb["facts"]:
            self.assertTrue(fact["evidence_urls"],
                            f"事实 {fact['fact_id']} 缺证据链接")

    def test_top3_hypotheses_present(self):
        self.assertEqual(len(self.wb["hypotheses"]), 3)
        self.assertEqual(self.wb["hypotheses"][0]["cause_code"],
                         "RECENT_RELEASE_REGRESSION")

    def test_suggested_actions_from_top_scenario_whitelist(self):
        actions = self.wb["suggested_actions"]
        self.assertEqual([a["action_type"] for a in actions],
                         ["rollback_release"])
        self.assertEqual(actions[0]["service"], "payment-api")

    def test_investigate_only_scenario_says_no_auto_action(self):
        run, tmp = make_run(
            metrics=ok_client({"instance_error_rates":
                               {"pod-1": 0.001, "pod-2": 0.002,
                                "pod-3": 0.4}}),
            release=ok_client({"version": "v41"}))   # 无窗口内发布
        self.addCleanup(tmp.cleanup)
        wb = build_workbench(run, alert=ALERT)
        self.assertEqual(wb["hypotheses"][0]["cause_code"],
                         "SINGLE_INSTANCE_ANOMALY")
        self.assertEqual(wb["suggested_actions"], [])
        self.assertIn("人工", wb["action_note"])


class TestMarkdownRender(unittest.TestCase):
    def test_render_contains_all_sections(self):
        run, tmp = make_run(trace=broken)
        self.addCleanup(tmp.cleanup)
        md = render_markdown(build_workbench(run, alert=ALERT))
        for section in ("时间线", "数据源", "事实", "Top-3 假设", "建议动作"):
            self.assertIn(section, md)
        self.assertIn("inc-001", md)
        self.assertIn("trace", md)              # 缺失源明示
        self.assertIn("rollback_release", md)
        self.assertIn("https://src/", md)       # 证据可回跳
