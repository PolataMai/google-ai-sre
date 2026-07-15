"""服务目录：试点准入约束 + 自治权限的最小粒度。

- 试点只收 Kubernetes 上的 Tier-1 无状态服务；
- 自治权限 scope = 服务 + 场景 + 动作 + 环境，格式固定；
- 新登记 scope 一律从 SHADOW 起步（渐进授权），禁止全局 L3。
"""
import unittest

from aisre.catalog import (AutonomyLevel, PilotEligibilityError, ServiceCatalog,
                           ServiceEntry, scope_key)


def make_entry(**overrides) -> ServiceEntry:
    kwargs = dict(
        name="payment-api",
        tier=1,
        stateless=True,
        platform="kubernetes",
        cluster="prod-cn-east",
        namespace="payment",
        workload="payment-api",
        owners=["team-payment"],
        slo={"error_rate_pct": 1.0, "latency_p99_ms": 300},
    )
    kwargs.update(overrides)
    return ServiceEntry(**kwargs)


class TestPilotEligibility(unittest.TestCase):
    def setUp(self):
        self.cat = ServiceCatalog()

    def test_tier1_stateless_k8s_service_accepted(self):
        self.cat.register(make_entry())
        self.assertEqual(self.cat.get("payment-api").tier, 1)

    def test_stateful_service_rejected(self):
        with self.assertRaises(PilotEligibilityError):
            self.cat.register(make_entry(name="order-db", stateless=False))

    def test_non_tier1_service_rejected(self):
        with self.assertRaises(PilotEligibilityError):
            self.cat.register(make_entry(name="batch-job", tier=2))

    def test_non_kubernetes_service_rejected(self):
        with self.assertRaises(PilotEligibilityError):
            self.cat.register(make_entry(name="legacy-vm", platform="vm"))

    def test_duplicate_registration_rejected(self):
        self.cat.register(make_entry())
        with self.assertRaises(ValueError):
            self.cat.register(make_entry())

    def test_get_unknown_service_raises(self):
        with self.assertRaises(KeyError):
            self.cat.get("ghost-svc")


class TestAutonomyScopes(unittest.TestCase):
    def setUp(self):
        self.cat = ServiceCatalog()
        self.cat.register(make_entry())

    def test_scope_key_format(self):
        key = scope_key("payment-api", "RECENT_RELEASE_REGRESSION",
                        "rollback_release", "prod-cn-east")
        self.assertEqual(
            key,
            "payment-api+RECENT_RELEASE_REGRESSION+rollback_release+prod-cn-east")

    def test_new_scope_starts_in_shadow(self):
        key = self.cat.grant_scope("payment-api", "CAPACITY_SATURATION",
                                   "scale_out")
        self.assertEqual(self.cat.autonomy_level(key), AutonomyLevel.SHADOW)

    def test_scope_for_unregistered_service_rejected(self):
        with self.assertRaises(KeyError):
            self.cat.grant_scope("ghost-svc", "CAPACITY_SATURATION", "scale_out")

    def test_scope_action_must_be_allowed_by_scenario(self):
        # 单实例异常场景没有可用动作，不能授予任何动作 scope
        with self.assertRaises(ValueError):
            self.cat.grant_scope("payment-api", "SINGLE_INSTANCE_ANOMALY",
                                 "scale_out")

    def test_unknown_scope_defaults_to_no_autonomy(self):
        self.assertIsNone(self.cat.autonomy_level("nonexistent+scope+key+x"))


class TestAutonomyTransitions(unittest.TestCase):
    """状态机:SHADOW → L2_APPROVAL → L3_AUTO;任意态可 SUSPENDED;不可跳级。"""

    def setUp(self):
        self.cat = ServiceCatalog()
        self.cat.register(make_entry())
        self.key = self.cat.grant_scope("payment-api", "CAPACITY_SATURATION",
                                        "scale_out")

    def test_promote_step_by_step(self):
        self.cat.set_level(self.key, AutonomyLevel.L2_APPROVAL)
        self.assertEqual(self.cat.autonomy_level(self.key),
                         AutonomyLevel.L2_APPROVAL)
        self.cat.set_level(self.key, AutonomyLevel.L3_AUTO)
        self.assertEqual(self.cat.autonomy_level(self.key),
                         AutonomyLevel.L3_AUTO)

    def test_skip_level_promotion_rejected(self):
        with self.assertRaises(ValueError):
            self.cat.set_level(self.key, AutonomyLevel.L3_AUTO)  # SHADOW 直跳 L3

    def test_suspend_from_any_level_and_no_direct_l3_recovery(self):
        self.cat.set_level(self.key, AutonomyLevel.L2_APPROVAL)
        self.cat.set_level(self.key, AutonomyLevel.SUSPENDED)
        self.assertEqual(self.cat.autonomy_level(self.key),
                         AutonomyLevel.SUSPENDED)
        with self.assertRaises(ValueError):
            self.cat.set_level(self.key, AutonomyLevel.L3_AUTO)  # 降级后禁直恢复 L3
        self.cat.set_level(self.key, AutonomyLevel.SHADOW)       # 只能回 SHADOW/L2
        self.assertEqual(self.cat.autonomy_level(self.key),
                         AutonomyLevel.SHADOW)

    def test_unknown_scope_cannot_set_level(self):
        with self.assertRaises(KeyError):
            self.cat.set_level("ghost+scope+key+x", AutonomyLevel.L2_APPROVAL)
