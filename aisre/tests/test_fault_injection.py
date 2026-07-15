"""故障注入演练:对两个 L2 动作注入执行后恶化,验证回滚通过率 100%,
并验证 Guardian 回滚熔断后,执行网关拒绝该 scope 的后续动作(端到端闭环)。

对应方案:两个动作均通过故障注入;故障注入与回滚演练通过率 100%。
本文件是集成演练——组合已各自 TDD 建成的 Guardian + gateway + catalog,
断言它们串起来仍守住"恶化即回滚 + 熔断 + 拒绝后续"的安全性质。
"""
import tempfile
import unittest

from aisre.catalog import AutonomyLevel, scope_key
from aisre.guardian import guard
from tests.test_actions import make_rollback_release, make_scale_out
from tests.test_gateway import GatewayHarness
from tests.test_guardian import FakeGuardianExecutor


class TestFaultInjectionDrill(unittest.TestCase):
    def test_both_actions_roll_back_under_fault(self):
        drills = [
            ("scale_out", make_scale_out(), ["crashloop"]),
            ("rollback_release", make_rollback_release(), ["new_error_signature"]),
        ]
        passed = 0
        report = []
        for name, plan, signals in drills:
            ex = FakeGuardianExecutor()
            suspended = []
            verdict = guard(plan, [{"regression_signals": signals}], ex,
                            on_rollback=lambda flag=suspended: flag.append(True))
            drill_ok = (verdict.rolled_back
                        and ex.rollbacks == [plan.action_id]
                        and suspended == [True])
            passed += drill_ok
            report.append((name, "PASS" if drill_ok else "FAIL"))
        self.assertEqual(passed, len(drills), report)   # 通过率 100%


class TestRollbackSuspendsScope(unittest.TestCase):
    def test_rollback_suspends_scope_gateway_rejects_followup(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = GatewayHarness(tmp)          # scope 已在 L2_APPROVAL
            key = scope_key("payment-api", "CAPACITY_SATURATION",
                            "scale_out", "prod-cn-east")

            first = h.approved_execute(make_scale_out(), "CAPACITY_SATURATION")
            self.assertTrue(first.executed)
            h.gateway.mark_completed(first.result["action_id"])

            # Guardian 注入故障 → 回滚 + 把 scope 熔断为 SUSPENDED
            ex = FakeGuardianExecutor()
            verdict = guard(
                make_scale_out(), [{"regression_signals": ["crashloop"]}], ex,
                on_rollback=lambda: h.catalog.set_level(
                    key, AutonomyLevel.SUSPENDED))
            self.assertTrue(verdict.rolled_back)
            self.assertEqual(h.catalog.autonomy_level(key),
                             AutonomyLevel.SUSPENDED)

            # 网关后续对该 scope 的动作在 autonomy 环被拒绝
            followup = h.approved_execute(
                make_scale_out(action_id="act-after", idempotency_key="k-after"),
                "CAPACITY_SATURATION")
            self.assertFalse(followup.executed)
            self.assertEqual(followup.stage, "autonomy")
