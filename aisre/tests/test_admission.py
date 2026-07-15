"""L3 准入门禁(把"开发完成 ≠ 指标达标"变成代码强制)。

核心性质:代码写完、测试全绿、甚至靠回放刷满 500 个 Shadow 案例,
都无法开 L3——只有真实试点数据(≥8 周且 ≥30 有效事故、真实 L2 执行、
业务指标对照基线达标、双人批准)能让门禁放行。

依据方案 §10 + §9 的 L3 条件逐条硬编码,任一不满足即拒绝。
"""
import unittest

from aisre.admission import PilotMetrics, evaluate_l3_admission


def full_pass(**overrides) -> PilotMetrics:
    """一组全部达标的试点指标;测试按需覆盖单项以验证每道门。"""
    base = dict(
        pilot_weeks=8.0,
        valid_incidents=30,
        shadow_cases=520,
        real_l2_executions=55,
        exact_match_total=500,
        exact_match_hits=499,          # 99.8% ≥ 99.6%
        weeks_continuous_compliant=8,
        ai_change_failure_rate=0.03,
        baseline_change_failure_rate=0.05,
        policy_bypasses=0,
        severe_wrong_actions=0,
        ai_caused_severe_incidents=0,
        fault_injection_pass_rate=1.0,
        dual_approved=True,
    )
    base.update(overrides)
    return PilotMetrics(**base)


def development_complete_only() -> PilotMetrics:
    """开发完成但没进过试点:代码/测试都好,回放刷满了 Shadow 案例,
    但没有真实执行、没有 8 周、没有业务对照、没有双人批准。"""
    return PilotMetrics(
        pilot_weeks=0.0, valid_incidents=0,
        shadow_cases=520,              # 回放能刷满
        real_l2_executions=0,
        exact_match_total=520, exact_match_hits=520,   # 回放里 100%
        weeks_continuous_compliant=0,
        ai_change_failure_rate=None,   # 没有真实变更数据
        baseline_change_failure_rate=0.05,
        policy_bypasses=0, severe_wrong_actions=0,
        ai_caused_severe_incidents=0,
        fault_injection_pass_rate=1.0,
        dual_approved=False)


class TestCoreProperty(unittest.TestCase):
    def test_development_complete_is_not_eligible(self):
        d = evaluate_l3_admission(development_complete_only())
        self.assertFalse(d.l3_eligible)
        # 明确指出缺的是真实试点数据,而非代码
        for gate in ("pilot_duration", "real_l2_executions",
                     "continuous_compliance", "business_vs_baseline",
                     "dual_approval"):
            self.assertIn(gate, d.blocking, gate)

    def test_all_gates_satisfied_is_eligible(self):
        d = evaluate_l3_admission(full_pass())
        self.assertTrue(d.l3_eligible, d.blocking)
        self.assertEqual(d.blocking, [])


class TestIndividualGates(unittest.TestCase):
    def assert_blocked(self, metrics, gate):
        d = evaluate_l3_admission(metrics)
        self.assertFalse(d.l3_eligible)
        self.assertIn(gate, d.blocking)

    def test_pilot_under_8_weeks_blocks(self):
        self.assert_blocked(full_pass(pilot_weeks=7.9), "pilot_duration")

    def test_fewer_than_30_incidents_blocks(self):
        self.assert_blocked(full_pass(valid_incidents=29), "pilot_duration")

    def test_under_500_cases_blocks(self):
        self.assert_blocked(full_pass(shadow_cases=499), "shadow_cases")

    def test_under_50_real_l2_blocks(self):
        self.assert_blocked(full_pass(real_l2_executions=49),
                            "real_l2_executions")

    def test_exact_match_below_99_6_blocks(self):
        # 500 案例只 497 匹配 = 99.4% < 99.6%
        self.assert_blocked(full_pass(exact_match_total=500,
                                      exact_match_hits=497), "exact_match")

    def test_exact_match_needs_at_least_500_sample(self):
        # 100 例全中(100%)也不够——样本量不足 500
        self.assert_blocked(full_pass(exact_match_total=100,
                                      exact_match_hits=100), "exact_match")

    def test_discontinuous_compliance_blocks(self):
        self.assert_blocked(full_pass(weeks_continuous_compliant=7),
                            "continuous_compliance")

    def test_policy_bypass_blocks(self):
        self.assert_blocked(full_pass(policy_bypasses=1), "safety")

    def test_severe_wrong_action_blocks(self):
        self.assert_blocked(full_pass(severe_wrong_actions=1), "safety")

    def test_any_ai_caused_severe_incident_hard_fails(self):
        d = evaluate_l3_admission(full_pass(ai_caused_severe_incidents=1))
        self.assertFalse(d.l3_eligible)
        self.assertIn("ai_caused_severe_incident", d.blocking)

    def test_ai_change_failure_rate_above_baseline_blocks(self):
        self.assert_blocked(full_pass(ai_change_failure_rate=0.06,
                                      baseline_change_failure_rate=0.05),
                            "business_vs_baseline")

    def test_missing_business_metric_blocks(self):
        self.assert_blocked(full_pass(ai_change_failure_rate=None),
                            "business_vs_baseline")

    def test_fault_injection_below_100pct_blocks(self):
        self.assert_blocked(full_pass(fault_injection_pass_rate=0.5),
                            "fault_injection")

    def test_missing_dual_approval_blocks(self):
        self.assert_blocked(full_pass(dual_approved=False), "dual_approval")


class TestDecisionShape(unittest.TestCase):
    def test_decision_serializable_with_per_gate_detail(self):
        import json
        d = evaluate_l3_admission(full_pass())
        parsed = json.loads(json.dumps(d.to_dict(), ensure_ascii=False))
        self.assertTrue(parsed["l3_eligible"])
        self.assertIn("pilot_duration", parsed["gates"])
        self.assertTrue(all("met" in g for g in parsed["gates"].values()))
