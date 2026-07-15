"""第 3–4 周端到端：Webhook → 事故 → 并行采集 → 证据库 → 事实引用证据。

验证三个模块的组合契约:intake 触发的工作流里,采集产出的证据入库后,
事实可以直接引用这些证据构建 Enrichment,覆盖率 100%。
"""
import tempfile
import unittest

from aisre.connectors import collect_context, default_connectors
from aisre.evidence_store import EvidenceStore
from aisre.intake import IntakeService
from aisre.schemas import Enrichment, Fact, evidence_coverage

WINDOW = ("2026-07-15T10:00:00Z", "2026-07-15T10:15:00Z")

WEBHOOK = {
    "alerts": [{
        "fingerprint": "abc123",
        "labels": {"alertname": "HighErrorRate", "service": "payment-api",
                   "severity": "critical"},
        "startsAt": "2026-07-15T10:08:00Z",
    }],
}


def ok_client(payload):
    def fetch(service, time_range):
        return {"url": f"https://src/q?svc={service}", "query": "q",
                "snapshot": payload}
    return fetch


def broken(service, time_range):
    raise ConnectionError("数据源不可用")


class TestIntakeToEvidenceFlow(unittest.TestCase):
    def test_full_flow_with_one_degraded_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = EvidenceStore(tmp)
            connectors = default_connectors(
                metrics=ok_client({"error_rate_after": 0.081}),
                logs=broken,                          # 单源故障不阻塞
                trace=ok_client({"spans": 3}),
                release=ok_client({"version": "v42", "previous": "v41"}),
                topology=ok_client({"deps": ["order-db"]}),
            )

            collected = {}

            def workflow(result):
                bundle = collect_context(result.alert.service, WINDOW,
                                         connectors)
                store.ingest(result.incident_id, bundle)
                collected[result.incident_id] = bundle

            svc = IntakeService(on_incident=workflow)
            result = svc.intake(WEBHOOK, "alertmanager")[0]

            # 采集：四源成功,logs 标缺失
            bundle = collected[result.incident_id]
            self.assertEqual(bundle.missing_sources, ["logs"])
            stored = store.list(result.incident_id)
            self.assertEqual(len(stored), 4)

            # 用库中证据构建 Enrichment,事实引用真实证据
            enr = Enrichment(incident_id=result.incident_id,
                             alert_received_at=result.alert.starts_at)
            for ev in stored:
                enr.add_evidence(ev)
            metric_ev = next(e for e in stored if e.source == "metrics")
            release_ev = next(e for e in stored if e.source == "release")
            enr.add_fact(Fact(
                fact_id="fact-1",
                text="错误率在 v42 发布后升至 8.1%",
                observed_at="2026-07-15T10:10:00Z",
                evidence_ids=[metric_ev.evidence_id, release_ev.evidence_id]))
            self.assertEqual(evidence_coverage(enr), 1.0)

            # 重复投递:不再触发第二次采集
            svc.intake(WEBHOOK, "alertmanager")
            self.assertEqual(len(collected), 1)
            self.assertEqual(len(store.list(result.incident_id)), 4)
