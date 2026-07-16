"""L3 准入门禁(把"开发完成 ≠ 指标达标"变成代码强制)。

分层(对应方案 §10"自动晋级程序只负责计算是否满足条件,最终晋级仍需双人批准"):
- evaluate_l3_admission:纯数据门计算(9 道),不含审批——审批不是数据,是行为;
- promote_to_l3:唯一的 L3 授权口——内部重算门禁(不接受外部传入的决定对象)
  + 校验两个互不相同的已验证人类主体 + 经 catalog 内部通道落级;
- catalog.set_level 对 L3_AUTO 一律拒绝——绕过门禁直升 L3 在 API 上不存在。

核心性质:开发完成(代码全绿、回放刷满案例)即便找来两个真人批准,
promote_to_l3 也拒绝——缺口只能靠真实试点数据补。
"""
import unittest

from aisre.admission import (AdmissionDenied, PilotMetrics, PromotionRecord,
                             derive_pilot_counts, evaluate_l3_admission,
                             promote_to_l3)
from aisre.catalog import AutonomyLevel, ServiceCatalog, ServiceEntry
from aisre.identity import IdentityAuthority

NOW = "2026-07-16T09:00:00Z"


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
    )
    base.update(overrides)
    return PilotMetrics(**base)


def development_complete_only() -> PilotMetrics:
    """开发完成但没进过试点:回放刷满了案例,但没有真实执行、
    没有 8 周、没有连续达标、没有业务对照数据。"""
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
        fault_injection_pass_rate=1.0)


class TestDataGates(unittest.TestCase):
    def test_development_complete_blocks_on_pilot_evidence(self):
        d = evaluate_l3_admission(development_complete_only())
        self.assertFalse(d.l3_eligible)
        for gate in ("pilot_duration", "real_l2_executions",
                     "continuous_compliance", "business_vs_baseline"):
            self.assertIn(gate, d.blocking, gate)

    def test_all_data_gates_satisfied(self):
        d = evaluate_l3_admission(full_pass())
        self.assertTrue(d.l3_eligible, d.blocking)
        self.assertEqual(d.blocking, [])

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

    def test_exact_match_boundary_498_of_500_passes(self):
        # 方案原文:500 个案例下至少 498 个精确匹配(点估计 99.6%)——
        # 边界值必须放行,且用整数运算避免浮点边界抖动
        d = evaluate_l3_admission(full_pass(exact_match_total=500,
                                            exact_match_hits=498))
        self.assertNotIn("exact_match", d.blocking)

    def test_exact_match_497_of_500_blocks(self):
        self.assert_blocked(full_pass(exact_match_total=500,
                                      exact_match_hits=497), "exact_match")

    def test_exact_match_needs_at_least_500_sample(self):
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
        self.assert_blocked(full_pass(ai_caused_severe_incidents=1),
                            "ai_caused_severe_incident")

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

    def test_decision_serializable_with_per_gate_detail(self):
        import json
        d = evaluate_l3_admission(full_pass())
        parsed = json.loads(json.dumps(d.to_dict(), ensure_ascii=False))
        self.assertTrue(parsed["l3_eligible"])
        self.assertIn("pilot_duration", parsed["gates"])
        self.assertTrue(all("met" in g for g in parsed["gates"].values()))


class TestPromotion(unittest.TestCase):
    """晋级是行为不是数据:必须经 promote_to_l3,双人批准是两个
    互不相同的已验证人类主体,不是布尔字段。"""

    def setUp(self):
        self.cat = ServiceCatalog()
        self.cat.register(ServiceEntry(
            name="payment-api", tier=1, stateless=True,
            platform="kubernetes", cluster="prod-cn-east",
            namespace="payment", workload="payment-api"))
        self.key = self.cat.grant_scope("payment-api", "CAPACITY_SATURATION",
                                        "scale_out")
        self.cat.set_level(self.key, AutonomyLevel.L2_APPROVAL)
        self.authority = IdentityAuthority(secret="adm-secret")
        self.alice = self.authority.issue("alice", "human", issued_at=NOW,
                                          ttl_seconds=3600)
        self.bob = self.authority.issue("bob", "human", issued_at=NOW,
                                        ttl_seconds=3600)
        self.agent = self.authority.issue("orchestrator", "agent",
                                          issued_at=NOW, ttl_seconds=3600)

    def promote(self, metrics, a=None, b=None):
        return promote_to_l3(
            catalog=self.cat, scope=self.key, metrics=metrics,
            authority=self.authority,
            approver_token_a=a or self.alice,
            approver_token_b=b or self.bob, now=NOW)

    def test_eligible_plus_two_humans_promotes(self):
        record = self.promote(full_pass())
        self.assertIsInstance(record, PromotionRecord)
        self.assertEqual(sorted(record.approvers), ["alice", "bob"])
        self.assertEqual(self.cat.autonomy_level(self.key),
                         AutonomyLevel.L3_AUTO)

    def test_dev_complete_denied_even_with_two_willing_humans(self):
        # 核心性质:找得到两个真人批准,也补不了真实试点数据的缺口
        with self.assertRaises(AdmissionDenied) as ctx:
            self.promote(development_complete_only())
        self.assertIn("pilot_duration", ctx.exception.blocking)
        self.assertEqual(self.cat.autonomy_level(self.key),
                         AutonomyLevel.L2_APPROVAL)   # 级别未动

    def test_same_person_twice_denied(self):
        alice_again = self.authority.issue("alice", "human", issued_at=NOW,
                                           ttl_seconds=1800)
        with self.assertRaises(AdmissionDenied):
            self.promote(full_pass(), a=self.alice, b=alice_again)

    def test_agent_approver_denied(self):
        with self.assertRaises(AdmissionDenied):
            self.promote(full_pass(), b=self.agent)

    def test_invalid_token_denied(self):
        with self.assertRaises(AdmissionDenied):
            self.promote(full_pass(), b="forged|token")

    def test_promotion_requires_l2_current_level(self):
        self.cat.set_level(self.key, AutonomyLevel.SUSPENDED)
        with self.assertRaises(AdmissionDenied):
            self.promote(full_pass())   # SUSPENDED 不可直升 L3

    def test_set_level_cannot_grant_l3_directly(self):
        # 绕过门禁的路径在 API 上不存在
        with self.assertRaises(ValueError):
            self.cat.set_level(self.key, AutonomyLevel.L3_AUTO)


class TestDerivePilotCounts(unittest.TestCase):
    """可派生的字段从记录算,不手填(其余字段显式标注为人工证词)。"""

    def test_counts_from_real_artifacts(self):
        import json
        import tempfile
        from pathlib import Path

        from aisre.evaluation import evaluate_replays
        from aisre.replay import ReplayCase, ShadowLog, replay_case
        from aisre.shadow import ShadowLedger, shadow_evaluate
        from tests.test_replay import CASE

        with tempfile.TemporaryDirectory() as tmp:
            # Shadow 案例:回放日志 2 条 + 生产 ledger 1 条
            log = ShadowLog(tmp)
            result = replay_case(ReplayCase.from_dict(CASE))
            log.record(result, at=NOW)
            log.record(replay_case(ReplayCase.from_dict(
                dict(CASE, case_id="case-002"))), at=NOW)
            ledger = ShadowLedger(tmp)
            ledger.append(shadow_evaluate(
                result.run, target=CASE["target"],
                expires_at=CASE["time_range"][1]))
            # 网关审计:2 次真实执行、1 次幂等重放、1 次拒绝
            audit = Path(tmp) / "gateway_audit.jsonl"
            records = [
                {"kind": "execution_attempt", "executed": True,
                 "idempotent_replay": False},
                {"kind": "execution_attempt", "executed": True,
                 "idempotent_replay": False},
                {"kind": "execution_attempt", "executed": True,
                 "idempotent_replay": True},    # 重放不算新执行
                {"kind": "execution_attempt", "executed": False,
                 "idempotent_replay": False},   # 拒绝不算
                {"kind": "red_button_control", "op": "kill"},
            ]
            audit.write_text("\n".join(json.dumps(r) for r in records),
                             encoding="utf-8")
            eval_report = evaluate_replays([result])

            derived = derive_pilot_counts(
                shadow_ledger=ledger, shadow_log=log,
                gateway_audit_dir=tmp, eval_report=eval_report)

        self.assertEqual(derived["shadow_cases"], 3)          # 2 + 1
        self.assertEqual(derived["real_l2_executions"], 2)    # 重放/拒绝不算
        self.assertEqual(derived["exact_match_total"], 1)
        self.assertEqual(derived["exact_match_hits"], 1)

    def test_missing_audit_file_counts_zero(self):
        import tempfile

        from aisre.evaluation import evaluate_replays
        from aisre.replay import ShadowLog
        from aisre.shadow import ShadowLedger

        with tempfile.TemporaryDirectory() as tmp:
            derived = derive_pilot_counts(
                shadow_ledger=ShadowLedger(tmp), shadow_log=ShadowLog(tmp),
                gateway_audit_dir=tmp, eval_report=evaluate_replays([]))
        self.assertEqual(derived["real_l2_executions"], 0)
        self.assertEqual(derived["shadow_cases"], 0)
