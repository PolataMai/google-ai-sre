"""安全执行网关(F08):所有写操作的唯一通道,检查链任一环失败即拒绝。

链路:红色按钮 → Agent 身份 → 动作契约(Schema/TTL/场景白名单)→ 幂等
→ 事故仍有效 → 自治 scope 等级 → 并发锁 → 限流 → Dry-run → 策略
→ L2 审批或 L3 资格 → 执行 → 审计。

关键性质:
- 默认拒绝、fail closed(依赖抛异常按拒绝处理,不冒泡);
- 提交者必须是 agent 主体,审批人必须是 human 主体;
- 幂等键重复直接返回上次结果,不二次执行;
- SHADOW/SUSPENDED/未授权 scope 一律拒绝执行;
- 每次尝试(含拒绝)都写审计。
"""
import json
import tempfile
import unittest
from pathlib import Path

from aisre.actions import approve
from aisre.catalog import AutonomyLevel, ServiceCatalog, ServiceEntry
from aisre.gateway import ExecutionGateway
from aisre.identity import IdentityAuthority
from aisre.policy import default_policy_set
from tests.test_actions import make_rollback_release, make_scale_out

NOW = "2026-07-15T10:12:00Z"


class FakeExecutor:
    def __init__(self, dry_run_ok=True, fail_dry_run_with=None):
        self.dry_runs = 0
        self.executions = 0
        self._ok = dry_run_ok
        self._raise = fail_dry_run_with

    def dry_run(self, plan):
        self.dry_runs += 1
        if self._raise:
            raise self._raise
        return self._ok, "server-side dry-run"

    def execute(self, plan):
        self.executions += 1
        return {"status": "applied", "action_id": plan.action_id}


class GatewayHarness:
    def __init__(self, tmp, level=AutonomyLevel.L2_APPROVAL,
                 incident_open=True, max_per_hour=10):
        self.catalog = ServiceCatalog()
        self.catalog.register(ServiceEntry(
            name="payment-api", tier=1, stateless=True,
            platform="kubernetes", cluster="prod-cn-east",
            namespace="payment", workload="payment-api"))
        for cause, action in (("CAPACITY_SATURATION", "scale_out"),
                              ("RECENT_RELEASE_REGRESSION",
                               "rollback_release")):
            key = self.catalog.grant_scope("payment-api", cause, action)
            if level != AutonomyLevel.SHADOW:
                self.catalog.set_level(key, AutonomyLevel.L2_APPROVAL)
                if level == AutonomyLevel.L3_AUTO:
                    self.catalog.set_level(key, AutonomyLevel.L3_AUTO)
                elif level == AutonomyLevel.SUSPENDED:
                    self.catalog.set_level(key, AutonomyLevel.SUSPENDED)
        self.authority = IdentityAuthority(secret="gw-secret")
        self.executors = {"scale_out": FakeExecutor(),
                          "rollback_release": FakeExecutor()}
        self.gateway = ExecutionGateway(
            catalog=self.catalog,
            policies=default_policy_set(allowed_namespaces=("payment",)),
            authority=self.authority,
            executors=self.executors,
            incident_is_open=lambda iid: incident_open,
            audit_dir=tmp,
            max_executions_per_hour=max_per_hour)
        self.agent_token = self.authority.issue(
            "ai-sre-orchestrator", "agent", issued_at=NOW, ttl_seconds=3600)
        self.human_token = self.authority.issue(
            "alice", "human", issued_at=NOW, ttl_seconds=3600)

    def approved_execute(self, plan, cause_code, **overrides):
        kwargs = dict(plan=plan, cause_code=cause_code,
                      agent_token=self.agent_token, now=NOW,
                      approval=approve(plan, approver="alice",
                                       approved_at=NOW),
                      approver_token=self.human_token)
        kwargs.update(overrides)
        return self.gateway.execute(**kwargs)


class TestHappyPath(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.h = GatewayHarness(self.tmp.name)

    def test_l2_approved_scale_out_executes(self):
        decision = self.h.approved_execute(make_scale_out(),
                                           "CAPACITY_SATURATION")
        self.assertTrue(decision.executed)
        self.assertEqual(decision.stage, "executed")
        self.assertEqual(decision.result["status"], "applied")
        self.assertEqual(self.h.executors["scale_out"].dry_runs, 1)
        self.assertEqual(self.h.executors["scale_out"].executions, 1)

    def test_l2_approved_rollback_executes(self):
        decision = self.h.approved_execute(make_rollback_release(),
                                           "RECENT_RELEASE_REGRESSION")
        self.assertTrue(decision.executed)

    def test_idempotent_replay_does_not_reexecute(self):
        first = self.h.approved_execute(make_scale_out(),
                                        "CAPACITY_SATURATION")
        self.h.gateway.mark_completed(first.result["action_id"])
        again = self.h.approved_execute(make_scale_out(),
                                        "CAPACITY_SATURATION")
        self.assertTrue(again.idempotent_replay)
        self.assertEqual(self.h.executors["scale_out"].executions, 1)

    def test_every_attempt_is_audited(self):
        self.h.approved_execute(make_scale_out(), "CAPACITY_SATURATION")
        self.h.gateway.execute(plan=make_scale_out(), cause_code="CAPACITY_SATURATION",
                               agent_token="bad|token", now=NOW)
        lines = (Path(self.tmp.name) / "gateway_audit.jsonl").read_text(
            encoding="utf-8").splitlines()
        records = [json.loads(l) for l in lines]
        self.assertTrue(any(r["executed"] for r in records))
        self.assertTrue(any(r["stage"] == "identity" and not r["allowed"]
                            for r in records))
        self.assertTrue(all("plan_hash" in r for r in records))


class TestChainDenials(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.h = GatewayHarness(self.tmp.name)

    def assert_denied(self, decision, stage):
        self.assertFalse(decision.executed)
        self.assertEqual(decision.stage, stage)

    def test_red_button_blocks_everything(self):
        self.h.gateway.kill(by="alice", at=NOW)
        d = self.h.approved_execute(make_scale_out(), "CAPACITY_SATURATION")
        self.assert_denied(d, "red_button")
        self.h.gateway.resume(by="alice", at=NOW)
        d2 = self.h.approved_execute(make_scale_out(), "CAPACITY_SATURATION")
        self.assertTrue(d2.executed)

    def test_human_submitter_denied(self):
        d = self.h.approved_execute(make_scale_out(), "CAPACITY_SATURATION",
                                    agent_token=self.h.human_token)
        self.assert_denied(d, "identity")

    def test_expired_plan_denied_at_contract(self):
        plan = make_scale_out(expires_at="2026-07-15T10:10:00Z")
        d = self.h.approved_execute(plan, "CAPACITY_SATURATION")
        self.assert_denied(d, "contract")

    def test_scenario_whitelist_enforced_at_contract(self):
        d = self.h.approved_execute(make_scale_out(),
                                    "RECENT_RELEASE_REGRESSION")
        self.assert_denied(d, "contract")

    def test_closed_incident_denied(self):
        h = GatewayHarness(self.tmp.name, incident_open=False)
        d = h.approved_execute(make_scale_out(), "CAPACITY_SATURATION")
        self.assert_denied(d, "incident_open")

    def test_shadow_scope_cannot_execute(self):
        h = GatewayHarness(self.tmp.name, level=AutonomyLevel.SHADOW)
        d = h.approved_execute(make_scale_out(), "CAPACITY_SATURATION")
        self.assert_denied(d, "autonomy")

    def test_suspended_scope_cannot_execute(self):
        h = GatewayHarness(self.tmp.name, level=AutonomyLevel.SUSPENDED)
        d = h.approved_execute(make_scale_out(), "CAPACITY_SATURATION")
        self.assert_denied(d, "autonomy")

    def test_l2_without_approval_denied(self):
        d = self.h.approved_execute(make_scale_out(), "CAPACITY_SATURATION",
                                    approval=None, approver_token=None)
        self.assert_denied(d, "authorization")

    def test_approval_hash_mismatch_denied(self):
        stale = approve(make_scale_out(), approver="alice", approved_at=NOW)
        mutated = make_scale_out(parameters={"original_replicas": 20,
                                             "target_replicas": 25})
        d = self.h.approved_execute(mutated, "CAPACITY_SATURATION",
                                    approval=stale)
        self.assert_denied(d, "authorization")

    def test_agent_as_approver_denied(self):
        d = self.h.approved_execute(make_scale_out(), "CAPACITY_SATURATION",
                                    approver_token=self.h.agent_token)
        self.assert_denied(d, "authorization")

    def test_l3_scope_executes_without_approval(self):
        h = GatewayHarness(self.tmp.name, level=AutonomyLevel.L3_AUTO)
        d = h.gateway.execute(plan=make_scale_out(),
                              cause_code="CAPACITY_SATURATION",
                              agent_token=h.agent_token, now=NOW)
        self.assertTrue(d.executed)

    def test_concurrent_action_on_same_service_denied(self):
        first = self.h.approved_execute(make_scale_out(),
                                        "CAPACITY_SATURATION")
        self.assertTrue(first.executed)
        second_plan = make_rollback_release(
            idempotency_key="inc-001-rollback-v9")
        d = self.h.approved_execute(second_plan,
                                    "RECENT_RELEASE_REGRESSION")
        self.assert_denied(d, "concurrency")
        self.h.gateway.mark_completed(first.result["action_id"])
        d2 = self.h.approved_execute(second_plan,
                                     "RECENT_RELEASE_REGRESSION")
        self.assertTrue(d2.executed)

    def test_rate_limit(self):
        h = GatewayHarness(self.tmp.name, max_per_hour=1)
        first = h.approved_execute(make_scale_out(), "CAPACITY_SATURATION")
        self.assertTrue(first.executed)
        h.gateway.mark_completed(first.result["action_id"])
        plan2 = make_scale_out(action_id="act-2", idempotency_key="k2")
        d = h.approved_execute(plan2, "CAPACITY_SATURATION")
        self.assert_denied(d, "rate_limit")

    def test_dry_run_failure_blocks_execution(self):
        self.h.executors["scale_out"]._ok = False
        d = self.h.approved_execute(make_scale_out(), "CAPACITY_SATURATION")
        self.assert_denied(d, "dry_run")
        self.assertEqual(self.h.executors["scale_out"].executions, 0)

    def test_executor_exception_fails_closed(self):
        self.h.executors["scale_out"]._raise = RuntimeError("adapter 崩溃")
        d = self.h.approved_execute(make_scale_out(), "CAPACITY_SATURATION")
        self.assert_denied(d, "dry_run")
        self.assertIn("adapter 崩溃", d.reason)

    def test_policy_denial(self):
        plan = make_scale_out(target={"cluster": "prod-cn-east",
                                      "namespace": "kube-system",
                                      "workload": "payment-api"})
        d = self.h.approved_execute(plan, "CAPACITY_SATURATION")
        self.assert_denied(d, "policy")
