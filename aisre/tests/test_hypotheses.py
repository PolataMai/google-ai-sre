"""Top-3 根因分析(F04):对三类场景确定性打分,输出三个候选假设。

- 永远输出全部三个场景的候选(无证据支持的作为低置信度待验证假设);
- 每个假设带支持/反对事实、验证步骤(取自场景定义)、置信度;
- 时序矛盾进反对证据:错误上升早于发布 → 发布事实是发布回归假设的反证;
- 同样的事实集合永远得到同样的排序与置信度(可回放评测 Top-3 召回率)。
"""
import unittest

from aisre.facts import extract_facts
from aisre.hypotheses import generate_hypotheses
from aisre.scenarios import get_scenario
from aisre.schemas import Evidence

WINDOW = ("2026-07-15T10:00:00Z", "2026-07-15T10:15:00Z")


def ev(eid, source, snapshot, window=WINDOW):
    return Evidence(evidence_id=eid, source=source, query="q",
                    time_range=window, url=f"https://src/{eid}",
                    snapshot=snapshot)


def release_regression_facts():
    return extract_facts([
        ev("m1", "metrics", {"error_rate_before": 0.002,
                             "error_rate_after": 0.081}),
        ev("r1", "release", {"version": "v42", "previous": "v41",
                             "deployed_at": "2026-07-15T10:05:00Z"}),
    ])


class TestTopThree(unittest.TestCase):
    def test_always_three_candidates_ranked(self):
        hyps = generate_hypotheses(release_regression_facts())
        self.assertEqual(len(hyps), 3)
        self.assertEqual([h.rank for h in hyps], [1, 2, 3])
        confidences = [h.confidence for h in hyps]
        self.assertEqual(confidences, sorted(confidences, reverse=True))

    def test_release_regression_ranks_first_with_deploy_and_rise(self):
        hyps = generate_hypotheses(release_regression_facts())
        top = hyps[0]
        self.assertEqual(top.cause_code, "RECENT_RELEASE_REGRESSION")
        self.assertGreaterEqual(top.confidence, 0.8)
        self.assertIn("fact-recent_deploy-1", top.evidence_for)
        self.assertIn("fact-error_rate_rise-1", top.evidence_for)

    def test_verification_steps_come_from_scenario(self):
        top = generate_hypotheses(release_regression_facts())[0]
        expected = list(get_scenario(top.cause_code).verification_steps)
        self.assertEqual(top.verification_steps, expected)

    def test_capacity_ranks_first_on_saturation(self):
        facts = extract_facts([
            ev("m1", "metrics", {"conn_pool_used_pct": 97.0}),
        ])
        top = generate_hypotheses(facts)[0]
        self.assertEqual(top.cause_code, "CAPACITY_SATURATION")
        self.assertIn("fact-capacity_saturation-1", top.evidence_for)

    def test_single_instance_ranks_first_on_outlier(self):
        facts = extract_facts([
            ev("m1", "metrics", {"instance_error_rates":
                                 {"pod-1": 0.001, "pod-2": 0.002,
                                  "pod-3": 0.4}}),
        ])
        top = generate_hypotheses(facts)[0]
        self.assertEqual(top.cause_code, "SINGLE_INSTANCE_ANOMALY")

    def test_error_rise_before_deploy_is_counter_evidence(self):
        # 错误 10:02 已出现,发布 10:05 才发生 → 发布事实进反对证据
        facts = extract_facts([
            ev("m1", "metrics", {"error_rate_before": 0.002,
                                 "error_rate_after": 0.081},
               window=("2026-07-15T10:00:00Z", "2026-07-15T10:02:00Z")),
            ev("r1", "release", {"version": "v42", "previous": "v41",
                                 "deployed_at": "2026-07-15T10:05:00Z"},
               window=("2026-07-15T10:00:00Z", "2026-07-15T10:15:00Z")),
        ])
        release_hyp = next(h for h in generate_hypotheses(facts)
                           if h.cause_code == "RECENT_RELEASE_REGRESSION")
        self.assertIn("fact-recent_deploy-1", release_hyp.evidence_against)
        self.assertLess(release_hyp.confidence, 0.5)

    def test_no_facts_yields_three_unverified_low_confidence(self):
        hyps = generate_hypotheses([])
        self.assertEqual(len(hyps), 3)
        for h in hyps:
            self.assertEqual(h.evidence_for, [])
            self.assertLessEqual(h.confidence, 0.2)

    def test_deterministic_output(self):
        a = generate_hypotheses(release_regression_facts())
        b = generate_hypotheses(release_regression_facts())
        self.assertEqual([(h.cause_code, h.confidence) for h in a],
                         [(h.cause_code, h.confidence) for h in b])
