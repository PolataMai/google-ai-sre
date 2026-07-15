"""线 3：裁决器——四种机制、证据链、守门校验、版本锚定降级。"""
import tempfile
import unittest

from rca import adjudicator
from rca.change_sources import collect_git_changes
from rca.code_graph import build_from_java_sources
from rca.log_forensics import analyze_log
from rca.schemas import (CandidateChange, ChangeType, ErrorFrame, EvidenceLink,
                         GuardrailViolation, Mechanism, Tier, TouchedRegion,
                         Verdict, validate_verdict)
from tests import helpers


class TestAdjudicate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.info = helpers.make_service_repo(cls.tmp.name)
        cls.graph = build_from_java_sources(cls.tmp.name)
        sigs = analyze_log(helpers.make_incident_log(cls.info),
                           "order-service", ["com.example"])
        cls.npe_sig = next(s for s in sigs if "NullPointer" in s.exception_type)
        cls.timeout_sig = next(s for s in sigs if "Timeout" in s.exception_type)
        cls.git_changes = collect_git_changes(
            cls.tmp.name, "order-service", helpers.NPE_FIRST_SEEN, 72)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    # ---- DIRECT → CONFIRMED ----

    def test_direct_hit_confirmed_with_evidence_chain(self):
        warnings = []
        v = adjudicator.adjudicate(
            self.npe_sig, self.git_changes, self.graph,
            repo=self.info["repo"], deployed_commit=self.info["bad_sha"],
            warnings=warnings)
        self.assertEqual(v.tier, Tier.CONFIRMED)
        self.assertEqual(v.mechanism, Mechanism.DIRECT)
        self.assertEqual(v.change_id, self.info["bad_sha"])
        kinds = [e.kind for e in v.evidence_chain]
        self.assertIn("stack_frame", kinds)
        self.assertIn("code_anchor", kinds)   # 版本锚定成功
        self.assertIn("diff_hunk", kinds)
        self.assertTrue(v.explanation_required)
        self.assertTrue(v.anchor_ok)
        self.assertEqual(warnings, [])
        # 锚定证据里必须包含发布 commit 上的真实源码行
        anchor = next(e for e in v.evidence_chain if e.kind == "code_anchor")
        self.assertIn("coupon.getDiscount()", anchor.detail)

    # ---- GRAPH → LIKELY ----

    def test_graph_hit_likely(self):
        """变更没有直接改报错行，但改动符号在故障可达代码集内。"""
        persist_node = self.graph.nodes[
            "com.example.order.service.OrderService.persist"]
        cand = CandidateChange(
            change_id="fakesha_graph", change_type=ChangeType.CODE,
            service="order-service", timestamp="2026-07-11T11:00:00+00:00",
            summary="调整落库逻辑",
            touched=[TouchedRegion(file=persist_node.file,
                                   line_start=persist_node.line_start + 1,
                                   line_end=persist_node.line_start + 1)])
        v = adjudicator.adjudicate(self.npe_sig, [cand], self.graph)
        self.assertEqual(v.tier, Tier.LIKELY)
        self.assertEqual(v.mechanism, Mechanism.GRAPH)
        self.assertEqual(v.change_id, "fakesha_graph")
        self.assertTrue(any(e.kind == "graph_path" for e in v.evidence_chain))

    # ---- TEMPORAL → LIKELY ----

    def test_temporal_config_with_key_match(self):
        cand = CandidateChange(
            change_id="CFG-9001", change_type=ChangeType.CONFIG,
            service="order-service", timestamp="2026-07-11T13:00:00+00:00",
            summary="调低券服务读取超时", relation="same-service",
            keys=["coupon"])  # 与报错符号 applyCoupon/getDiscount 的词面无关也应命中 message
        v = adjudicator.adjudicate(self.npe_sig, [cand], self.graph)
        self.assertEqual(v.tier, Tier.LIKELY)
        self.assertEqual(v.mechanism, Mechanism.TEMPORAL)
        self.assertTrue(any(e.kind == "key_match" for e in v.evidence_chain))

    def test_unrelated_service_config_not_blamed(self):
        cand = CandidateChange(
            change_id="CFG-8801", change_type=ChangeType.CONFIG,
            service="inventory-service", timestamp="2026-07-11T09:00:00+00:00",
            summary="无关服务配置", relation="other-service", keys=["cache.ttl"])
        v = adjudicator.adjudicate(self.timeout_sig, [cand], self.graph)
        self.assertEqual(v.tier, Tier.HYPOTHESIS)
        self.assertIsNone(v.change_id)

    # ---- NONE → HYPOTHESIS（不许硬凑）----

    def test_no_intersection_yields_hypothesis_with_next_actions(self):
        v = adjudicator.adjudicate(
            self.timeout_sig, self.git_changes, self.graph,
            repo=self.info["repo"], deployed_commit=self.info["bad_sha"])
        self.assertEqual(v.tier, Tier.HYPOTHESIS)
        self.assertIsNone(v.change_id, "无交集时禁止归因任何变更")
        self.assertTrue(v.next_actions)

    # ---- 版本锚定失败 → 降级 ----

    def test_anchor_failure_downgrades_to_likely(self):
        sig = self.npe_sig
        drifted = type(sig)(
            fingerprint=sig.fingerprint, exception_type=sig.exception_type,
            message_sample=sig.message_sample, service=sig.service,
            top_business_frame=sig.top_business_frame,
            frames=[ErrorFrame(class_fqn=f.class_fqn, method=f.method,
                               file=f.file, line=999, is_business=f.is_business)
                    for f in sig.frames],
            first_seen=sig.first_seen, last_seen=sig.last_seen, count=sig.count)
        cand = CandidateChange(
            change_id="fakesha_drift", change_type=ChangeType.CODE,
            service="order-service", timestamp="2026-07-11T11:00:00+00:00",
            summary="行号漂移的变更",
            touched=[TouchedRegion(
                file=f"{helpers.SRC}/service/PricingService.java",
                line_start=990, line_end=1010)])
        warnings = []
        v = adjudicator.adjudicate(
            drifted, [cand], self.graph,
            repo=self.info["repo"], deployed_commit=self.info["bad_sha"],
            warnings=warnings)
        self.assertEqual(v.tier, Tier.LIKELY)
        self.assertEqual(v.mechanism, Mechanism.DIRECT)
        self.assertFalse(v.anchor_ok)
        self.assertTrue(warnings and "锚定" in warnings[0])

    # ---- 排序：DIRECT 优先于 TEMPORAL ----

    def test_direct_outranks_temporal(self):
        cfg = CandidateChange(
            change_id="CFG-9002", change_type=ChangeType.CONFIG,
            service="order-service", timestamp="2026-07-11T14:00:00+00:00",
            summary="同期配置变更", relation="same-service", keys=["coupon"])
        v = adjudicator.adjudicate(
            self.npe_sig, [cfg] + self.git_changes, self.graph,
            repo=self.info["repo"], deployed_commit=self.info["bad_sha"])
        self.assertEqual(v.change_id, self.info["bad_sha"])
        self.assertEqual(v.mechanism, Mechanism.DIRECT)
        # 但配置变更仍出现在候选排名里，供人工复核
        ids = [c.change_id for c in v.ranked_candidates]
        self.assertIn("CFG-9002", ids)


class TestGuardrails(unittest.TestCase):
    def test_hypothesis_with_change_id_rejected(self):
        v = Verdict(fingerprint="fp", tier=Tier.HYPOTHESIS,
                    mechanism=Mechanism.NONE, change_id="sha123",
                    next_actions=["x"])
        with self.assertRaises(GuardrailViolation):
            validate_verdict(v)

    def test_hypothesis_without_next_actions_rejected(self):
        v = Verdict(fingerprint="fp", tier=Tier.HYPOTHESIS, mechanism=Mechanism.NONE)
        with self.assertRaises(GuardrailViolation):
            validate_verdict(v)

    def test_confirmed_requires_direct_mechanism(self):
        v = Verdict(fingerprint="fp", tier=Tier.CONFIRMED,
                    mechanism=Mechanism.GRAPH, change_id="sha123",
                    evidence_chain=[EvidenceLink("stack_frame", "x"),
                                    EvidenceLink("diff_hunk", "y")])
        with self.assertRaises(GuardrailViolation):
            validate_verdict(v)

    def test_confirmed_requires_full_evidence_chain(self):
        v = Verdict(fingerprint="fp", tier=Tier.CONFIRMED,
                    mechanism=Mechanism.DIRECT, change_id="sha123",
                    evidence_chain=[EvidenceLink("stack_frame", "x")])
        with self.assertRaises(GuardrailViolation):
            validate_verdict(v)


class TestMitigation(unittest.TestCase):
    def test_confirmed_rollback_first(self):
        cand = CandidateChange(
            change_id="abcdef1234567890", change_type=ChangeType.CODE,
            service="s", timestamp="2026-07-11T10:00:00+00:00", summary="坏提交")
        v = Verdict(fingerprint="fp", tier=Tier.CONFIRMED,
                    mechanism=Mechanism.DIRECT, change_id=cand.change_id,
                    evidence_chain=[EvidenceLink("stack_frame", "x"),
                                    EvidenceLink("diff_hunk", "y")])
        out = adjudicator.build_mitigation([v], [cand])
        self.assertIn("回滚", out[0])
        self.assertIn("abcdef123456", out[0])

    def test_hypothesis_only_gives_containment(self):
        v = Verdict(fingerprint="fp", tier=Tier.HYPOTHESIS,
                    mechanism=Mechanism.NONE, next_actions=["x"])
        out = adjudicator.build_mitigation([v], [])
        self.assertEqual(len(out), 1)
        self.assertIn("兜底", out[0])


if __name__ == "__main__":
    unittest.main()
