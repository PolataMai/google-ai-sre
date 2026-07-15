"""事件 Schema：事实必须带证据，假设允许待验证，证据覆盖率可从数据算出。

对应 F03（事实与证据服务）：
- 没有 evidence_ids 的内容不能进入 facts；
- 引用不存在的证据视同无证据；
- 假设的 cause_code 必须是已注册场景；
- 证据覆盖率 = 有至少一个可解析证据的事实数 ÷ 全部事实数（对外部载入的数据也要能算）。
"""
import unittest

from aisre.schemas import (Enrichment, Evidence, Fact, Hypothesis,
                           MissingEvidence, UnknownEvidence,
                           evidence_coverage, validate_enrichment)


def make_evidence(eid="metric-1"):
    return Evidence(
        evidence_id=eid,
        source="metrics",
        query="sum(rate(http_errors_total[5m]))",
        time_range=("2026-07-15T10:00:00Z", "2026-07-15T10:15:00Z"),
        url="https://grafana.example.com/d/abc?from=x&to=y",
        snapshot={"before": 0.002, "after": 0.081},
    )


def make_fact(evidence_ids=("metric-1",)):
    return Fact(
        fact_id="fact-101",
        text="错误率在 v42 发布后 5 分钟从 0.2% 升至 8.1%",
        observed_at="2026-07-15T10:10:00Z",
        evidence_ids=list(evidence_ids),
    )


class TestFactEvidenceBinding(unittest.TestCase):
    def setUp(self):
        self.enr = Enrichment(incident_id="inc-001", alert_received_at="2026-07-15T10:08:00Z")
        self.enr.add_evidence(make_evidence())

    def test_fact_with_resolvable_evidence_is_accepted(self):
        self.enr.add_fact(make_fact())
        self.assertEqual(len(self.enr.facts), 1)

    def test_fact_without_evidence_is_rejected(self):
        with self.assertRaises(MissingEvidence):
            self.enr.add_fact(make_fact(evidence_ids=()))

    def test_fact_with_unknown_evidence_id_is_rejected(self):
        with self.assertRaises(UnknownEvidence):
            self.enr.add_fact(make_fact(evidence_ids=("metric-1", "ghost-9")))

    def test_duplicate_fact_id_is_rejected(self):
        self.enr.add_fact(make_fact())
        with self.assertRaises(ValueError):
            self.enr.add_fact(make_fact())


class TestHypothesis(unittest.TestCase):
    def setUp(self):
        self.enr = Enrichment(incident_id="inc-001", alert_received_at="2026-07-15T10:08:00Z")
        self.enr.add_evidence(make_evidence())
        self.enr.add_fact(make_fact())

    def test_hypothesis_with_facts_and_valid_cause_code(self):
        self.enr.add_hypothesis(Hypothesis(
            rank=1, cause_code="RECENT_RELEASE_REGRESSION",
            evidence_for=["fact-101"], evidence_against=[],
            verification_steps=["compare_canary_baseline"], confidence=0.93,
        ))
        self.assertEqual(self.enr.hypotheses[0].rank, 1)

    def test_unverified_hypothesis_without_facts_is_allowed(self):
        # 无证据的推测只能作为待验证假设存在——允许进 hypotheses，不允许进 facts
        self.enr.add_hypothesis(Hypothesis(
            rank=2, cause_code="CAPACITY_SATURATION",
            evidence_for=[], evidence_against=[],
            verification_steps=["compare_load_vs_capacity"], confidence=0.3,
        ))
        self.assertEqual(len(self.enr.hypotheses), 1)

    def test_unknown_cause_code_is_rejected(self):
        with self.assertRaises(ValueError):
            self.enr.add_hypothesis(Hypothesis(
                rank=1, cause_code="DNS_MELTDOWN",
                evidence_for=[], evidence_against=[],
                verification_steps=[], confidence=0.5,
            ))

    def test_hypothesis_referencing_unknown_fact_is_rejected(self):
        with self.assertRaises(ValueError):
            self.enr.add_hypothesis(Hypothesis(
                rank=1, cause_code="RECENT_RELEASE_REGRESSION",
                evidence_for=["fact-404"], evidence_against=[],
                verification_steps=[], confidence=0.9,
            ))

    def test_confidence_out_of_range_is_rejected(self):
        with self.assertRaises(ValueError):
            self.enr.add_hypothesis(Hypothesis(
                rank=1, cause_code="RECENT_RELEASE_REGRESSION",
                evidence_for=[], evidence_against=[],
                verification_steps=[], confidence=1.5,
            ))


class TestCoverageAndValidation(unittest.TestCase):
    def test_coverage_is_one_for_api_built_enrichment(self):
        enr = Enrichment(incident_id="inc-001", alert_received_at="2026-07-15T10:08:00Z")
        enr.add_evidence(make_evidence())
        enr.add_fact(make_fact())
        self.assertEqual(evidence_coverage(enr), 1.0)

    def test_coverage_over_externally_loaded_data(self):
        # 外部 JSON 载入不经过 add_fact 校验，覆盖率必须暴露缺口
        enr = Enrichment.from_dict({
            "incident_id": "inc-002",
            "alert_received_at": "2026-07-15T11:00:00Z",
            "evidences": [make_evidence().to_dict()],
            "facts": [
                make_fact().to_dict(),
                {"fact_id": "fact-102", "text": "疑似连接池耗尽",
                 "observed_at": "2026-07-15T11:02:00Z", "evidence_ids": []},
            ],
            "hypotheses": [],
        })
        self.assertEqual(evidence_coverage(enr), 0.5)

    def test_validate_enrichment_reports_violations(self):
        enr = Enrichment.from_dict({
            "incident_id": "inc-003",
            "alert_received_at": "2026-07-15T11:00:00Z",
            "evidences": [],
            "facts": [{"fact_id": "f1", "text": "无证据事实",
                       "observed_at": "2026-07-15T11:01:00Z", "evidence_ids": ["ghost"]}],
            "hypotheses": [{"rank": 1, "cause_code": "NOT_A_SCENARIO",
                            "evidence_for": [], "evidence_against": [],
                            "verification_steps": [], "confidence": 0.5}],
        })
        violations = validate_enrichment(enr)
        self.assertTrue(any("f1" in v for v in violations))
        self.assertTrue(any("NOT_A_SCENARIO" in v for v in violations))

    def test_coverage_of_empty_facts_is_zero(self):
        enr = Enrichment(incident_id="inc-004", alert_received_at="2026-07-15T11:00:00Z")
        self.assertEqual(evidence_coverage(enr), 0.0)

    def test_roundtrip_to_dict_from_dict(self):
        enr = Enrichment(incident_id="inc-001", alert_received_at="2026-07-15T10:08:00Z")
        enr.add_evidence(make_evidence())
        enr.add_fact(make_fact())
        again = Enrichment.from_dict(enr.to_dict())
        self.assertEqual(again.to_dict(), enr.to_dict())
