"""场景定义：MVP 三类诊断场景的注册表与约束。

对应 F04/F06 的前提：cause_code 是封闭枚举，每个场景声明
检测信号、验证步骤、允许的 L2 动作——单实例异常在 MVP 只调查不动作。
"""
import unittest

from aisre.scenarios import (CauseCode, ScenarioDef, UnknownScenario,
                             get_scenario, list_scenarios)


class TestScenarioRegistry(unittest.TestCase):
    def test_exactly_three_pilot_scenarios(self):
        codes = {s.cause_code for s in list_scenarios()}
        self.assertEqual(codes, {
            CauseCode.RECENT_RELEASE_REGRESSION,
            CauseCode.CAPACITY_SATURATION,
            CauseCode.SINGLE_INSTANCE_ANOMALY,
        })

    def test_release_regression_only_allows_rollback(self):
        s = get_scenario(CauseCode.RECENT_RELEASE_REGRESSION)
        self.assertEqual(s.allowed_actions, ("rollback_release",))

    def test_capacity_saturation_only_allows_scale_out(self):
        s = get_scenario(CauseCode.CAPACITY_SATURATION)
        self.assertEqual(s.allowed_actions, ("scale_out",))

    def test_single_instance_anomaly_is_investigate_only(self):
        s = get_scenario(CauseCode.SINGLE_INSTANCE_ANOMALY)
        self.assertEqual(s.allowed_actions, ())

    def test_every_scenario_declares_signals_and_verification(self):
        for s in list_scenarios():
            self.assertIsInstance(s, ScenarioDef)
            self.assertGreater(len(s.detection_signals), 0, s.cause_code)
            self.assertGreater(len(s.verification_steps), 0, s.cause_code)

    def test_get_scenario_accepts_string_code(self):
        s = get_scenario("CAPACITY_SATURATION")
        self.assertEqual(s.cause_code, CauseCode.CAPACITY_SATURATION)

    def test_unknown_code_raises(self):
        with self.assertRaises(UnknownScenario):
            get_scenario("DNS_MELTDOWN")
