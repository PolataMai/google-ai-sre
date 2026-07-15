"""证据存储（F03 的存储层）：按事故落盘、追加式、完整性哈希、供事实引用。

验收对应:
- 每条证据带 URL、查询参数、时间范围、快照,存储原样保全;
- 追加式:同一 evidence_id 不允许覆盖(证据不可篡改的第一道防线);
- 完整性:每条证据存 sha256,verify 能发现落盘后被篡改的记录;
- 重启进程后可从磁盘恢复(新实例读同一目录看到全部证据)。
"""
import json
import tempfile
import unittest
from pathlib import Path

from aisre.connectors import default_connectors, collect_context
from aisre.evidence_store import DuplicateEvidence, EvidenceStore
from aisre.schemas import Evidence

WINDOW = ("2026-07-15T10:00:00Z", "2026-07-15T10:15:00Z")


def make_evidence(eid="metric-1", snapshot=None):
    return Evidence(
        evidence_id=eid, source="metrics", query="rate(errors[5m])",
        time_range=WINDOW, url="https://grafana/d/abc",
        snapshot=snapshot or {"before": 0.002, "after": 0.081})


def ok_client(payload):
    def fetch(service, time_range):
        return {"url": "https://src/q", "query": "q", "snapshot": payload}
    return fetch


class TestEvidenceStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = EvidenceStore(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_add_and_list(self):
        self.store.add("inc-001", make_evidence())
        evs = self.store.list("inc-001")
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0].evidence_id, "metric-1")
        self.assertEqual(evs[0].snapshot, {"before": 0.002, "after": 0.081})

    def test_duplicate_id_rejected(self):
        self.store.add("inc-001", make_evidence())
        with self.assertRaises(DuplicateEvidence):
            self.store.add("inc-001", make_evidence(snapshot={"tampered": 1}))

    def test_same_id_different_incident_allowed(self):
        self.store.add("inc-001", make_evidence())
        self.store.add("inc-002", make_evidence())   # 证据按事故隔离
        self.assertEqual(len(self.store.list("inc-002")), 1)

    def test_persistence_across_instances(self):
        self.store.add("inc-001", make_evidence())
        reopened = EvidenceStore(self.tmp.name)
        evs = reopened.list("inc-001")
        self.assertEqual(evs[0].url, "https://grafana/d/abc")

    def test_list_unknown_incident_is_empty(self):
        self.assertEqual(self.store.list("inc-ghost"), [])

    def test_verify_clean_store(self):
        self.store.add("inc-001", make_evidence())
        self.assertEqual(self.store.verify("inc-001"), [])

    def test_verify_detects_tampering(self):
        self.store.add("inc-001", make_evidence())
        path = Path(self.tmp.name) / "inc-001.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data[0]["evidence"]["snapshot"] = {"after": 0.001}   # 篡改快照
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        reopened = EvidenceStore(self.tmp.name)
        self.assertEqual(reopened.verify("inc-001"), ["metric-1"])

    def test_ingest_context_bundle(self):
        connectors = default_connectors(
            metrics=ok_client({"error_rate": 0.08}),
            logs=ok_client({"lines": 12}),
            trace=ok_client({"spans": 3}),
            release=ok_client({"version": "v42"}),
            topology=ok_client({"deps": ["db"]}),
        )
        bundle = collect_context("payment-api", WINDOW, connectors)
        stored = self.store.ingest("inc-001", bundle)
        self.assertEqual(stored, 5)
        self.assertEqual({e.source for e in self.store.list("inc-001")},
                         {"metrics", "logs", "trace", "release", "topology"})
