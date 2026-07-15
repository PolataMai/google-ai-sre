"""Guardian(F09):执行后持续观测 SLI,成功放行,恶化/超时自动回滚 + 熔断。

对应方案的失败处理:自动执行补偿动作(plan.rollback)、标记动作失败、
停止该服务后续自动动作(on_rollback 熔断回调)并升级人工。

观测序列模型(与 replay 的时间切片同精神):Guardian 消费一串观测快照
逐个裁决——一旦全部成功条件达成即放行,一旦出现恶化信号立即回滚,
窗口内始终无法确认成功则保守回滚(fail closed:拿不到 SLI 也回滚)。
"""
import unittest

from aisre.guardian import GuardianVerdict, guard
from tests.test_actions import make_rollback_release, make_scale_out


class FakeGuardianExecutor:
    def __init__(self):
        self.rollbacks = []

    def rollback(self, plan):
        self.rollbacks.append(plan.action_id)
        return {"status": "compensated", "compensation": plan.rollback}

# 成功条件的求值语义已移到契约层 SuccessCriterion,单元覆盖见
# tests/test_success_criteria.py;此处只测 guard() 的编排行为。


class TestGuardSuccess(unittest.TestCase):
    def setUp(self):
        self.ex = FakeGuardianExecutor()

    def test_success_on_first_observation(self):
        v = guard(make_scale_out(),
                  [{"error_rate": 0.005, "slo_burn_rate": 1.2}], self.ex)
        self.assertEqual(v.outcome, "success")
        self.assertFalse(v.rolled_back)
        self.assertEqual(v.observations_consumed, 1)
        self.assertEqual(self.ex.rollbacks, [])

    def test_success_after_recovery(self):
        obs = [{"error_rate": 0.05, "slo_burn_rate": 3.0},
               {"error_rate": 0.02, "slo_burn_rate": 2.4},
               {"error_rate": 0.004, "slo_burn_rate": 1.1}]
        v = guard(make_scale_out(), obs, self.ex)
        self.assertEqual(v.outcome, "success")
        self.assertEqual(v.observations_consumed, 3)

    def test_rollback_release_success_on_boolean_flags(self):
        v = guard(make_rollback_release(),
                  [{"sli_recovered_5m": True, "no_new_error_signature": True}],
                  self.ex)
        self.assertEqual(v.outcome, "success")

    def test_partial_criteria_not_success(self):
        v = guard(make_scale_out(),
                  [{"error_rate": 0.004, "slo_burn_rate": 3.0}], self.ex)
        self.assertEqual(v.outcome, "rolled_back")   # 单观测未全满足 → 超时回滚


class TestGuardRollback(unittest.TestCase):
    def setUp(self):
        self.ex = FakeGuardianExecutor()

    def test_regression_triggers_immediate_rollback(self):
        obs = [{"error_rate": 0.05, "slo_burn_rate": 3.0},
               {"regression_signals": ["crashloop"]},
               {"error_rate": 0.004, "slo_burn_rate": 1.0}]   # 不该被消费
        v = guard(make_scale_out(), obs, self.ex)
        self.assertEqual(v.outcome, "rolled_back")
        self.assertIn("crashloop", v.reason)
        self.assertEqual(v.observations_consumed, 2)
        self.assertEqual(self.ex.rollbacks, ["act-20260715-001"])
        self.assertEqual(v.rollback_result["status"], "compensated")

    def test_timeout_rollback_when_never_healthy(self):
        obs = [{"error_rate": 0.05, "slo_burn_rate": 3.0},
               {"error_rate": 0.04, "slo_burn_rate": 2.8}]
        v = guard(make_scale_out(), obs, self.ex)
        self.assertEqual(v.outcome, "rolled_back")
        self.assertEqual(self.ex.rollbacks, ["act-20260715-001"])

    def test_empty_observations_fail_closed(self):
        v = guard(make_scale_out(), [], self.ex)
        self.assertEqual(v.outcome, "rolled_back")   # 拿不到 SLI 也止血
        self.assertEqual(v.observations_consumed, 0)
        self.assertEqual(self.ex.rollbacks, ["act-20260715-001"])

    def test_on_rollback_fires_only_on_rollback(self):
        fired = []
        guard(make_scale_out(), [{"regression_signals": ["dependency_overload"]}],
              self.ex, on_rollback=lambda: fired.append("suspended"))
        self.assertEqual(fired, ["suspended"])

        fired2 = []
        guard(make_scale_out(), [{"error_rate": 0.004, "slo_burn_rate": 1.0}],
              self.ex, on_rollback=lambda: fired2.append("suspended"))
        self.assertEqual(fired2, [])

    def test_verdict_is_dataclass(self):
        v = guard(make_scale_out(), [], self.ex)
        self.assertIsInstance(v, GuardianVerdict)
