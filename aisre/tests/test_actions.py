"""动作契约（F06/F07 的 Schema 层）：

- 只有 scale_out / rollback_release 两个类型化动作；
- scale_out 扩容幅度 10%–25%，回滚必须恢复原副本数；
- 计划有 TTL，dry_run 强制；
- 审批绑定 action_id + plan_hash，参数变化后原审批立即失效；
- 动作类型必须落在所属场景的 allowed_actions 白名单内。
"""
import unittest

from aisre.actions import (ActionPlan, Approval, approve, is_approval_valid,
                           validate_action_plan)
from aisre.scenarios import get_scenario


def make_scale_out(**overrides) -> ActionPlan:
    kwargs = dict(
        action_id="act-20260715-001",
        incident_id="inc-001",
        action_type="scale_out",
        service="payment-api",
        target={"cluster": "prod-cn-east", "namespace": "payment",
                "workload": "payment-api"},
        parameters={"original_replicas": 20, "target_replicas": 24},
        preconditions=["quota_available", "no_active_rollout",
                       "current_replicas=20"],
        success_criteria=["error_rate<1%", "slo_burn_rate<2"],
        rollback={"action_type": "restore_replicas", "target_replicas": 20},
        idempotency_key="inc-001-scale-out-v1",
        expires_at="2026-07-15T10:20:00Z",
        dry_run_required=True,
    )
    kwargs.update(overrides)
    return ActionPlan(**kwargs)


def make_rollback_release(**overrides) -> ActionPlan:
    kwargs = dict(
        action_id="act-20260715-002",
        incident_id="inc-001",
        action_type="rollback_release",
        service="payment-api",
        target={"cluster": "prod-cn-east", "namespace": "payment",
                "workload": "payment-api"},
        parameters={"current_version": "v42", "rollback_to_version": "v41"},
        preconditions=["release_correlated", "no_db_schema_change",
                       "artifact_v41_available"],
        success_criteria=["sli_recovered_5m", "no_new_error_signature"],
        rollback={"action_type": "redeploy_version", "version": "v42"},
        idempotency_key="inc-001-rollback-v1",
        expires_at="2026-07-15T10:20:00Z",
        dry_run_required=True,
    )
    kwargs.update(overrides)
    return ActionPlan(**kwargs)


NOW = "2026-07-15T10:12:00Z"


class TestValidation(unittest.TestCase):
    def test_valid_scale_out_passes(self):
        self.assertEqual(validate_action_plan(make_scale_out(), now=NOW), [])

    def test_valid_rollback_release_passes(self):
        self.assertEqual(validate_action_plan(make_rollback_release(), now=NOW), [])

    def test_unknown_action_type_rejected(self):
        plan = make_scale_out(action_type="delete_namespace")
        self.assertTrue(any("action_type" in v for v in
                            validate_action_plan(plan, now=NOW)))

    def test_scale_out_above_25_percent_rejected(self):
        plan = make_scale_out(parameters={"original_replicas": 20,
                                          "target_replicas": 26})  # +30%
        self.assertTrue(any("25%" in v for v in validate_action_plan(plan, now=NOW)))

    def test_scale_out_below_10_percent_rejected(self):
        plan = make_scale_out(parameters={"original_replicas": 20,
                                          "target_replicas": 21})  # +5%
        self.assertTrue(any("10%" in v for v in validate_action_plan(plan, now=NOW)))

    def test_scale_in_rejected(self):
        plan = make_scale_out(parameters={"original_replicas": 20,
                                          "target_replicas": 18})
        self.assertNotEqual(validate_action_plan(plan, now=NOW), [])

    def test_scale_out_rollback_must_restore_original(self):
        plan = make_scale_out(rollback={"action_type": "restore_replicas",
                                        "target_replicas": 22})
        self.assertTrue(any("恢复原副本数" in v for v in
                            validate_action_plan(plan, now=NOW)))

    def test_expired_plan_rejected(self):
        plan = make_scale_out(expires_at="2026-07-15T10:10:00Z")
        self.assertTrue(any("过期" in v for v in
                            validate_action_plan(plan, now="2026-07-15T10:12:00Z")))

    def test_dry_run_not_required_rejected(self):
        plan = make_scale_out(dry_run_required=False)
        self.assertTrue(any("dry_run" in v for v in
                            validate_action_plan(plan, now=NOW)))

    def test_empty_preconditions_rejected(self):
        plan = make_scale_out(preconditions=[])
        self.assertNotEqual(validate_action_plan(plan, now=NOW), [])

    def test_empty_success_criteria_rejected(self):
        plan = make_scale_out(success_criteria=[])
        self.assertNotEqual(validate_action_plan(plan, now=NOW), [])

    def test_action_must_match_scenario_whitelist(self):
        # 发布回归场景不允许 scale_out
        scenario = get_scenario("RECENT_RELEASE_REGRESSION")
        violations = validate_action_plan(make_scale_out(), now=NOW,
                                          scenario=scenario)
        self.assertTrue(any("白名单" in v for v in violations))

    def test_action_allowed_by_scenario_passes(self):
        scenario = get_scenario("RECENT_RELEASE_REGRESSION")
        self.assertEqual(
            validate_action_plan(make_rollback_release(), now=NOW,
                                 scenario=scenario), [])


class TestPlanHashAndApproval(unittest.TestCase):
    def test_plan_hash_is_deterministic(self):
        self.assertEqual(make_scale_out().plan_hash(), make_scale_out().plan_hash())

    def test_plan_hash_changes_when_parameters_change(self):
        a = make_scale_out()
        b = make_scale_out(parameters={"original_replicas": 20,
                                       "target_replicas": 25})
        self.assertNotEqual(a.plan_hash(), b.plan_hash())

    def test_approval_binds_action_id_and_plan_hash(self):
        plan = make_scale_out()
        appr = approve(plan, approver="alice", approved_at=NOW)
        self.assertIsInstance(appr, Approval)
        self.assertTrue(is_approval_valid(plan, appr))

    def test_approval_invalidated_by_parameter_change(self):
        plan = make_scale_out()
        appr = approve(plan, approver="alice", approved_at=NOW)
        mutated = make_scale_out(parameters={"original_replicas": 20,
                                             "target_replicas": 25})
        self.assertFalse(is_approval_valid(mutated, appr))

    def test_approval_not_transferable_to_other_action(self):
        appr = approve(make_scale_out(), approver="alice", approved_at=NOW)
        other = make_scale_out(action_id="act-20260715-999")
        self.assertFalse(is_approval_valid(other, appr))

    def test_roundtrip_to_dict_from_dict_preserves_hash(self):
        plan = make_scale_out()
        again = ActionPlan.from_dict(plan.to_dict())
        self.assertEqual(again.plan_hash(), plan.plan_hash())
