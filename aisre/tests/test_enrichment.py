"""告警丰富编排:采集 → 入库 → 事实 → Top-3 → 校验 → 发布,90 秒预算内。

验收对应:
- 单源失败仍发布(partial=True,缺失源标注),不阻塞;
- 发布结果通过 validate_enrichment 守门(违规清单必须为空);
- 缺失源事后追加(refresh):补证据、重算事实与假设,不换 incident_id;
- p95 口径 = enrichment_published_at - alert_received_at(墙钟,不是模型耗时);
- 分段计时可导出(collect/aggregate/reason/validate 各一段)。
"""
import tempfile
import unittest

from aisre.connectors import default_connectors
from aisre.enrichment import (enrichment_latency_seconds, p95_seconds,
                              refresh_missing, run_enrichment)
from aisre.evidence_store import EvidenceStore
from aisre.intake import Alert
from aisre.schemas import validate_enrichment

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
    raise ConnectionError("数据源不可用")


def make_connectors(**overrides):
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
    return default_connectors(**clients)


class TestRunEnrichment(unittest.TestCase):
    def run_full(self, connectors):
        self.tmp = tempfile.TemporaryDirectory()
        store = EvidenceStore(self.tmp.name)
        run = run_enrichment(
            incident_id="inc-001", alert=ALERT, time_range=WINDOW,
            connectors=connectors, store=store,
            published_at="2026-07-15T10:09:20Z")
        self.addCleanup(self.tmp.cleanup)
        return run, store

    def test_full_run_publishes_valid_enrichment(self):
        run, store = self.run_full(make_connectors())
        self.assertFalse(run.partial)
        self.assertEqual(run.violations, [])
        self.assertEqual(validate_enrichment(run.enrichment), [])
        self.assertEqual(run.enrichment.enrichment_published_at,
                         "2026-07-15T10:09:20Z")
        self.assertEqual(len(run.enrichment.hypotheses), 3)
        self.assertEqual(run.enrichment.hypotheses[0].cause_code,
                         "RECENT_RELEASE_REGRESSION")
        # 证据已入库
        self.assertEqual(len(store.list("inc-001")), 5)

    def test_partial_publish_on_source_failure(self):
        run, _ = self.run_full(make_connectors(release=broken))
        self.assertTrue(run.partial)
        self.assertEqual(run.missing_sources, ["release"])
        self.assertEqual(run.violations, [])
        # 没有发布证据 → 无法归因发布,三个候选都是低置信待验证
        self.assertLessEqual(run.enrichment.hypotheses[0].confidence, 0.2)
        release_hyp = next(h for h in run.enrichment.hypotheses
                           if h.cause_code == "RECENT_RELEASE_REGRESSION")
        self.assertEqual(release_hyp.evidence_for, [])

    def test_stage_timings_recorded(self):
        run, _ = self.run_full(make_connectors())
        for stage in ("collect", "aggregate", "reason", "validate"):
            self.assertIn(stage, run.stage_seconds)
            self.assertGreaterEqual(run.stage_seconds[stage], 0.0)

    def test_refresh_missing_appends_and_regenerates(self):
        run, store = self.run_full(make_connectors(release=broken))
        fixed = make_connectors()   # release 恢复
        refreshed = refresh_missing(
            run, connectors=fixed, store=store,
            published_at="2026-07-15T10:10:30Z")
        self.assertEqual(refreshed.missing_sources, [])
        self.assertFalse(refreshed.partial)
        self.assertEqual(refreshed.enrichment.incident_id, "inc-001")
        self.assertEqual(len(store.list("inc-001")), 5)
        # 发布证据补齐后,发布回归重新登顶
        self.assertEqual(refreshed.enrichment.hypotheses[0].cause_code,
                         "RECENT_RELEASE_REGRESSION")
        self.assertEqual(refreshed.enrichment.enrichment_published_at,
                         "2026-07-15T10:10:30Z")
        self.assertEqual(refreshed.violations, [])


class TestLatencyMetrics(unittest.TestCase):
    def test_latency_is_wall_clock_from_alert_to_publish(self):
        run, _ = TestRunEnrichment.run_full(TestRunEnrichment(), make_connectors())
        # 10:08:00 告警 → 10:09:20 发布 = 80 秒
        self.assertEqual(enrichment_latency_seconds(run.enrichment), 80.0)

    def test_p95_nearest_rank(self):
        durations = [float(x) for x in range(1, 101)]   # 1..100
        self.assertEqual(p95_seconds(durations), 95.0)

    def test_p95_empty_is_none(self):
        self.assertIsNone(p95_seconds([]))
