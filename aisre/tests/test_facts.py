"""事实抽取:从证据快照确定性地提取事实(告警丰富的"聚合去重验证"环节)。

规则化抽取是 MVP 阶段对"LLM 推理"的确定性替身:每条事实必然绑定产生它的
证据,阈值是模块常量,同样的证据永远得到同样的事实(可回放、可评测)。
"""
import unittest

from aisre.facts import ExtractedFact, extract_facts
from aisre.schemas import Evidence

WINDOW = ("2026-07-15T10:00:00Z", "2026-07-15T10:15:00Z")


def ev(eid, source, snapshot):
    return Evidence(evidence_id=eid, source=source, query="q",
                    time_range=WINDOW, url=f"https://src/{eid}",
                    snapshot=snapshot)


class TestExtraction(unittest.TestCase):
    def test_error_rate_rise_fact(self):
        facts = extract_facts([ev("m1", "metrics",
                                  {"error_rate_before": 0.002,
                                   "error_rate_after": 0.081})])
        kinds = [f.kind for f in facts]
        self.assertIn("error_rate_rise", kinds)
        fact = next(f for f in facts if f.kind == "error_rate_rise")
        self.assertEqual(fact.fact.evidence_ids, ["m1"])
        self.assertIn("0.2%", fact.fact.text)
        self.assertIn("8.1%", fact.fact.text)

    def test_no_rise_no_fact(self):
        facts = extract_facts([ev("m1", "metrics",
                                  {"error_rate_before": 0.002,
                                   "error_rate_after": 0.003})])
        self.assertEqual([f.kind for f in facts], [])

    def test_capacity_saturation_fact(self):
        facts = extract_facts([ev("m1", "metrics",
                                  {"cpu_utilization_pct": 93.0})])
        fact = next(f for f in facts if f.kind == "capacity_saturation")
        self.assertIn("cpu", fact.fact.text)
        self.assertIn("93", fact.fact.text)

    def test_single_instance_outlier_fact(self):
        facts = extract_facts([ev("m1", "metrics", {
            "instance_error_rates": {"pod-1": 0.002, "pod-2": 0.003,
                                     "pod-3": 0.31}})])
        fact = next(f for f in facts if f.kind == "single_instance_outlier")
        self.assertIn("pod-3", fact.fact.text)
        self.assertEqual(fact.meta["instance"], "pod-3")

    def test_uniform_instances_no_outlier(self):
        facts = extract_facts([ev("m1", "metrics", {
            "instance_error_rates": {"pod-1": 0.30, "pod-2": 0.31,
                                     "pod-3": 0.29}})])
        self.assertEqual([f.kind for f in facts], [])

    def test_deploy_within_window_fact(self):
        facts = extract_facts([ev("r1", "release", {
            "version": "v42", "previous": "v41",
            "deployed_at": "2026-07-15T10:05:00Z"})])
        fact = next(f for f in facts if f.kind == "recent_deploy")
        self.assertIn("v42", fact.fact.text)
        self.assertEqual(fact.meta["deployed_at"], "2026-07-15T10:05:00Z")

    def test_deploy_outside_window_ignored(self):
        facts = extract_facts([ev("r1", "release", {
            "version": "v42", "previous": "v41",
            "deployed_at": "2026-07-15T08:00:00Z"})])
        self.assertEqual([f.kind for f in facts], [])

    def test_fact_ids_deterministic_and_unique(self):
        evidences = [
            ev("m1", "metrics", {"error_rate_before": 0.002,
                                 "error_rate_after": 0.081}),
            ev("r1", "release", {"version": "v42", "previous": "v41",
                                 "deployed_at": "2026-07-15T10:05:00Z"}),
        ]
        once = extract_facts(evidences)
        twice = extract_facts(evidences)
        self.assertEqual([f.fact.fact_id for f in once],
                         [f.fact.fact_id for f in twice])
        ids = [f.fact.fact_id for f in once]
        self.assertEqual(len(ids), len(set(ids)))
