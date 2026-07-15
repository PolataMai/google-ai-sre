"""端到端：合成故障 → CLI 全流程 → 报告断言。

场景：
- NPE 签名：应 CONFIRMED，归因到 bad 提交，止血建议为回滚该提交；
- 超时签名：窗口内无任何相交变更，应 HYPOTHESIS 且不归因（不硬凑）；
- 知识库：预置同指纹历史案例应命中；--write-back 后新结论入库。
"""
import json
import tempfile
import unittest
from pathlib import Path

from rca import cli
from rca.knowledge_base import KnowledgeBase
from rca.log_forensics import analyze_log
from tests import helpers


class TestEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.info = helpers.make_service_repo(str(root / "order-service"))

        cls.log_path = root / "app.log"
        cls.log_path.write_text(helpers.make_incident_log(cls.info), encoding="utf-8")

        cls.audit_path = root / "audit.json"
        cls.audit_path.write_text(json.dumps(helpers.AUDIT_ENTRIES), encoding="utf-8")

        cls.alert_path = root / "alert.json"
        cls.alert_path.write_text(json.dumps({
            "incident_id": "INC-20260711-001",
            "service": "order-service",
            "alert_time": helpers.ALERT_TIME,
            "deployed_commit": cls.info["bad_sha"],
            "business_packages": ["com.example"],
        }), encoding="utf-8")

        # 预置知识库：同指纹的历史故障
        sigs = analyze_log(helpers.make_incident_log(cls.info),
                           "order-service", ["com.example"])
        cls.npe_fp = next(s.fingerprint for s in sigs
                          if "NullPointer" in s.exception_type)
        cls.kb_path = root / "kb.json"
        cls.kb_path.write_text(json.dumps({cls.npe_fp: [{
            "incident_id": "INC-20260301-007", "date": "2026-03-01T08:00:00",
            "tier": "CONFIRMED", "root_cause": "历史上同位置的空券 NPE，当时回滚修复",
            "change_id": "oldsha", "notes": "历史案例"}]}), encoding="utf-8")

        cls.report_md = root / "report.md"
        cls.report_json = root / "report.json"
        cls.exit_code = cli.main([
            "run",
            "--alert", str(cls.alert_path),
            "--logs", str(cls.log_path),
            "--repo", cls.info["repo"],
            "--audit", str(cls.audit_path),
            "--kb", str(cls.kb_path),
            "--write-back",
            "--out", str(cls.report_md),
            "--json-out", str(cls.report_json),
        ])
        cls.report = json.loads(cls.report_json.read_text(encoding="utf-8"))
        cls.md = cls.report_md.read_text(encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _verdict(self, fp_contains_exc: str) -> dict:
        sig = next(s for s in self.report["signatures"]
                   if fp_contains_exc in s["exception_type"])
        return next(v for v in self.report["verdicts"]
                    if v["fingerprint"] == sig["fingerprint"])

    def test_exit_code_zero(self):
        self.assertEqual(self.exit_code, 0)

    def test_npe_confirmed_and_attributed_to_bad_commit(self):
        v = self._verdict("NullPointer")
        self.assertEqual(v["tier"], "CONFIRMED")
        self.assertEqual(v["mechanism"], "DIRECT")
        self.assertEqual(v["change_id"], self.info["bad_sha"])
        kinds = [e["kind"] for e in v["evidence_chain"]]
        self.assertEqual(kinds[:3], ["stack_frame", "code_anchor", "diff_hunk"])

    def test_timeout_is_hypothesis_without_attribution(self):
        v = self._verdict("Timeout")
        self.assertEqual(v["tier"], "HYPOTHESIS")
        self.assertIsNone(v["change_id"])
        self.assertTrue(v["next_actions"])

    def test_mitigation_rollback_first(self):
        first = self.report["mitigation"][0]
        self.assertIn("回滚", first)
        self.assertIn(self.info["bad_sha"][:12], first)

    def test_unrelated_config_listed_but_not_blamed(self):
        cand_ids = [c["change_id"] for c in self.report["candidates"]]
        self.assertIn("CFG-8801", cand_ids)       # 窗口内 → 列出
        self.assertNotIn("CFG-8802", cand_ids)    # 错误之后 → 排除
        blamed = {v["change_id"] for v in self.report["verdicts"]}
        self.assertNotIn("CFG-8801", blamed)

    def test_kb_history_matched(self):
        self.assertTrue(any(k["incident_id"] == "INC-20260301-007"
                            for k in self.report["kb_matches"]))

    def test_kb_write_back(self):
        entries = KnowledgeBase(str(self.kb_path)).lookup(self.npe_fp)
        ids = [e.incident_id for e in entries]
        self.assertIn("INC-20260711-001", ids)

    def test_markdown_sections(self):
        for section in ("止血建议", "根因结论", "候选变更", "历史相似故障"):
            self.assertIn(section, self.md)
        self.assertIn("CONFIRMED", self.md)
        self.assertIn("HYPOTHESIS", self.md)
        self.assertIn("行为差异解释：**待补齐**", self.md)


if __name__ == "__main__":
    unittest.main()
