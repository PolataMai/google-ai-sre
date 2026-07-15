"""知识库：查询、回写闭环、HYPOTHESIS 不回写、幂等。"""
import tempfile
import unittest
from pathlib import Path

from rca.knowledge_base import KnowledgeBase
from rca.schemas import (CandidateChange, ChangeType, ErrorSignature,
                         EvidenceLink, Mechanism, RcaReport, Tier, Verdict)


def _report(tier: Tier, fp: str = "fp001") -> RcaReport:
    sig = ErrorSignature(
        fingerprint=fp, exception_type="java.lang.NullPointerException",
        message_sample="npe", service="order-service", top_business_frame=None,
        first_seen="2026-07-11 14:23:05", last_seen="2026-07-11 14:24:30", count=3)
    cand = CandidateChange(
        change_id="sha_bad", change_type=ChangeType.CODE, service="order-service",
        timestamp="2026-07-11T10:00:00+00:00", summary="简化券折扣计算路径")
    if tier == Tier.HYPOTHESIS:
        v = Verdict(fingerprint=fp, tier=tier, mechanism=Mechanism.NONE,
                    next_actions=["查流量"])
    else:
        v = Verdict(fingerprint=fp, tier=tier, mechanism=Mechanism.DIRECT,
                    change_id="sha_bad",
                    evidence_chain=[EvidenceLink("stack_frame", "f"),
                                    EvidenceLink("diff_hunk", "d")])
    return RcaReport(incident_id="INC-1", service="order-service",
                     alert_time="2026-07-11T14:25:00", deployed_commit="sha_bad",
                     signatures=[sig], candidates=[cand], verdicts=[v])


class TestKnowledgeBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.kb_path = str(Path(self.tmp.name) / "kb.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_lookup_empty(self):
        kb = KnowledgeBase(self.kb_path)
        self.assertEqual(kb.lookup("nope"), [])

    def test_write_back_and_reload(self):
        kb = KnowledgeBase(self.kb_path)
        self.assertEqual(kb.write_back(_report(Tier.CONFIRMED)), 1)
        # 重新加载后可查询（持久化闭环）
        kb2 = KnowledgeBase(self.kb_path)
        entries = kb2.lookup("fp001")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].change_id, "sha_bad")
        self.assertIn("NullPointerException", entries[0].root_cause)

    def test_hypothesis_not_written(self):
        kb = KnowledgeBase(self.kb_path)
        self.assertEqual(kb.write_back(_report(Tier.HYPOTHESIS)), 0)
        self.assertEqual(KnowledgeBase(self.kb_path).lookup("fp001"), [])

    def test_add_entry_refines_existing_incident(self):
        """--write-back 初稿 → kb-add 定案精修：同 incident 覆盖而非重复。"""
        kb = KnowledgeBase(self.kb_path)
        kb.write_back(_report(Tier.CONFIRMED))
        kb.add_entry("fp001", {
            "incident_id": "INC-1", "date": "2026-07-11T14:25:00",
            "tier": "CONFIRMED",
            "root_cause": "bad 提交删除空券判断，空券路径直接解引用导致 NPE",
            "change_id": "sha_bad", "notes": "反驳验证通过"})
        entries = KnowledgeBase(self.kb_path).lookup("fp001")
        self.assertEqual(len(entries), 1)
        self.assertIn("空券判断", entries[0].root_cause)

    def test_idempotent_per_incident(self):
        kb = KnowledgeBase(self.kb_path)
        kb.write_back(_report(Tier.CONFIRMED))
        self.assertEqual(kb.write_back(_report(Tier.CONFIRMED)), 0)
        self.assertEqual(len(KnowledgeBase(self.kb_path).lookup("fp001")), 1)


if __name__ == "__main__":
    unittest.main()
