"""策略引擎(OPA 替身):策略判断与程序执行分离。

- 默认拒绝(fail closed):空规则集/无匹配即 deny;
- 规则数据驱动,决策带策略版本(审计要求记录 policy_version);
- MVP 三条内置策略:动作目录白名单、目标命名空间白名单、扩容爆炸半径。
"""
import unittest

from aisre.policy import PolicySet, default_policy_set, evaluate
from tests.test_actions import make_rollback_release, make_scale_out


def make_input(plan):
    return {"action_type": plan.action_type, "service": plan.service,
            "target": plan.target, "parameters": plan.parameters}


class TestPolicyEngine(unittest.TestCase):
    def setUp(self):
        self.policies = default_policy_set(
            allowed_namespaces=("payment",), max_scale_increase_pct=25)

    def test_compliant_scale_out_allowed(self):
        decision = evaluate(self.policies, make_input(make_scale_out()))
        self.assertTrue(decision.allow)
        self.assertEqual(decision.reasons, [])
        self.assertEqual(decision.policy_version, self.policies.version)

    def test_compliant_rollback_allowed(self):
        decision = evaluate(self.policies,
                            make_input(make_rollback_release()))
        self.assertTrue(decision.allow)

    def test_namespace_outside_allowlist_denied(self):
        plan = make_scale_out(target={"cluster": "prod-cn-east",
                                      "namespace": "kube-system",
                                      "workload": "payment-api"})
        decision = evaluate(self.policies, make_input(plan))
        self.assertFalse(decision.allow)
        self.assertTrue(any("kube-system" in r for r in decision.reasons))

    def test_blast_radius_denied(self):
        plan = make_scale_out(parameters={"original_replicas": 20,
                                          "target_replicas": 30})   # +50%
        decision = evaluate(self.policies, make_input(plan))
        self.assertFalse(decision.allow)

    def test_unknown_action_denied(self):
        plan = make_scale_out(action_type="drain_node")
        decision = evaluate(self.policies, make_input(plan))
        self.assertFalse(decision.allow)

    def test_empty_policy_set_denies_by_default(self):
        empty = PolicySet(version="v0-empty", rules=[])
        decision = evaluate(empty, make_input(make_scale_out()))
        self.assertFalse(decision.allow)
        self.assertTrue(any("默认拒绝" in r for r in decision.reasons))
