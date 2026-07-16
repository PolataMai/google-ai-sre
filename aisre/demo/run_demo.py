"""端到端演示:告警接入 → 并行采集 → 证据库 → 告警丰富 → 动作规划 → 审批 → 基线。

运行:python3 demo/run_demo.py
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aisre.actions import ActionPlan, approve, is_approval_valid, validate_action_plan
from aisre.admission import (AdmissionDenied, PilotMetrics, derive_pilot_counts,
                             evaluate_l3_admission, promote_to_l3)
from aisre.baseline import ChangeRecord, IncidentRecord, compute_baseline
from aisre.board import build_board
from aisre.catalog import ServiceCatalog, ServiceEntry
from aisre.connectors import default_connectors
from aisre.enrichment import (enrichment_latency_seconds, refresh_missing,
                              run_enrichment)
from aisre.evaluation import evaluate_replays
from aisre.evidence_store import EvidenceStore
from aisre.gateway import ExecutionGateway
from aisre.gold import GoldStore, accept, suggest_from_execution
from aisre.guardian import guard
from aisre.identity import IdentityAuthority
from aisre.policy import default_policy_set
from aisre.intake import IntakeService
from aisre.replay import ReplayCase, ShadowLog, replay_case
from aisre.scenarios import get_scenario
from aisre.schemas import evidence_coverage
from aisre.shadow import ShadowLedger, shadow_evaluate
from aisre.workbench import build_workbench, render_markdown

NOW = "2026-07-15T10:12:00Z"
WINDOW = ("2026-07-15T10:00:00Z", "2026-07-15T10:15:00Z")

WEBHOOK = {
    "alerts": [{
        "fingerprint": "abc123",
        "labels": {"alertname": "HighErrorRate", "service": "payment-api",
                   "severity": "critical"},
        "startsAt": "2026-07-15T10:08:00Z",
    }],
}


def step(title):
    print(f"\n=== {title} ===")


def ok_client(payload):
    def fetch(service, time_range):
        return {"url": f"https://src.example.com/q?svc={service}",
                "query": f"query({service})", "snapshot": payload}
    return fetch


def broken_logs(service, time_range):
    raise ConnectionError("日志平台暂时不可用")


def main():
    # 1. 服务目录:登记试点服务并授予 SHADOW scope
    step("1. 服务目录与自治 scope")
    cat = ServiceCatalog()
    cat.register(ServiceEntry(
        name="payment-api", tier=1, stateless=True, platform="kubernetes",
        cluster="prod-cn-east", namespace="payment", workload="payment-api",
        owners=["team-payment"], slo={"error_rate_pct": 1.0}))
    key = cat.grant_scope("payment-api", "RECENT_RELEASE_REGRESSION",
                          "rollback_release")
    print(f"scope: {key} -> {cat.autonomy_level(key).value}")

    # 2. 告警接入:Webhook → 统一 incident_id,重复投递幂等
    step("2. 告警接入(F01)")
    svc = IntakeService()
    result = svc.intake(WEBHOOK, "alertmanager")[0]
    dup = svc.intake(WEBHOOK, "alertmanager")[0]
    print(f"incident: {result.incident_id}(created={result.created}),"
          f"重复投递 created={dup.created}")

    # 3. 告警丰富编排:采集(logs 故障)→ 事实 → Top-3 → 校验 → 部分发布
    step("3. 告警丰富编排(F02/F03/F04,logs 故障先发布)")
    tmp = tempfile.mkdtemp(prefix="aisre-evidence-")
    store = EvidenceStore(tmp)
    clients = dict(
        metrics=ok_client({"error_rate_before": 0.002, "error_rate_after": 0.081}),
        logs=broken_logs,
        trace=ok_client({"error_spans": 37}),
        release=ok_client({"version": "v42", "previous": "v41",
                           "deployed_at": "2026-07-15T10:05:00Z"}),
        topology=ok_client({"upstream": ["gateway"], "downstream": ["order-db"]}),
    )
    run = run_enrichment(
        incident_id=result.incident_id, alert=result.alert,
        time_range=WINDOW, connectors=default_connectors(**clients),
        store=store, published_at="2026-07-15T10:09:20Z")
    first_publish_latency = enrichment_latency_seconds(run.enrichment)
    print(f"部分发布: partial={run.partial},缺失源: {run.missing_sources},"
          f"守门违规: {run.violations or '无'}")
    print(f"告警→首次发布延迟: {first_publish_latency:.0f}s,"
          f"完整性校验: 被篡改 {store.verify(result.incident_id) or '无'}")

    # 追加:logs 恢复后补齐缺失源,重算事实与 Top-3
    clients["logs"] = ok_client({"error_lines": 240})
    run = refresh_missing(run, connectors=default_connectors(**clients),
                          store=store, published_at="2026-07-15T10:10:30Z")
    enr = run.enrichment
    print(f"追加后: 缺失源 {run.missing_sources or '无'},"
          f"事实 {len(enr.facts)} 条,证据覆盖率 {evidence_coverage(enr):.0%},"
          f"Top-1: {enr.hypotheses[0].cause_code}"
          f"(置信 {enr.hypotheses[0].confidence:.2f})")

    # 4. 事故工作台:单一视图渲染
    step("4. 事故工作台(F05)")
    wb = build_workbench(run, alert=result.alert)
    md = render_markdown(wb)
    print("\n".join(md.splitlines()[:14]))
    print(f"…(共 {len(md.splitlines())} 行,含数据源/事实/假设/建议动作)")

    # 5. 动作规划:按场景白名单生成 rollback_release 计划并校验
    step("5. 动作契约校验")
    scenario = get_scenario(enr.hypotheses[0].cause_code)
    plan = ActionPlan(
        action_id="act-20260715-001", incident_id=result.incident_id,
        action_type="rollback_release", service="payment-api",
        target={"cluster": "prod-cn-east", "namespace": "payment",
                "workload": "payment-api"},
        parameters={"current_version": "v42", "rollback_to_version": "v41"},
        preconditions=["release_correlated", "no_db_schema_change",
                       "artifact_v41_available"],
        success_criteria=["sli_recovered_5m", "no_new_error_signature"],
        rollback={"action_type": "redeploy_version", "version": "v42"},
        idempotency_key=f"{result.incident_id}-rollback-v1",
        expires_at="2026-07-15T10:20:00Z")
    violations = validate_action_plan(plan, now=NOW, scenario=scenario)
    print(f"违规: {violations or '无'},plan_hash: {plan.plan_hash()[:16]}…")

    # 6. 审批绑定 plan_hash:参数变化后审批失效
    step("6. 审批绑定")
    appr = approve(plan, approver="alice", approved_at=NOW)
    print(f"原计划审批有效: {is_approval_valid(plan, appr)}")
    plan.parameters["rollback_to_version"] = "v40"
    print(f"参数被改后审批有效: {is_approval_valid(plan, appr)}")

    # 7. 90 天基线
    step("7. 90 天基线")
    incidents = [IncidentRecord(**d) for d in _load("incidents.jsonl")]
    changes = [ChangeRecord(**d) for d in _load("changes.jsonl")]
    report = compute_baseline(incidents=incidents, changes=changes,
                              as_of="2026-07-15T00:00:00Z")
    mttm = report.to_dict()["by_service_scenario"]
    print(json.dumps(mttm, ensure_ascii=False, indent=2))

    # 8. 回放评测 + Gold 回流 + Shadow 日志
    step("8. 时间切片回放与评测(F11)")
    cases = [ReplayCase.from_dict(d) for d in _load("replay_cases.jsonl")]
    shadow = ShadowLog(tmp)
    results = []
    for case in cases:
        r = replay_case(case)
        shadow.record(r, at="2026-07-15T12:00:00Z")
        results.append(r)
    eval_report = evaluate_replays(results)
    print(f"回放 {eval_report.total_cases} 例: "
          f"Top-3 召回 {eval_report.top3_recall:.0%},"
          f"Top-1 准确 {eval_report.top1_accuracy:.0%},"
          f"L2 精确匹配 {eval_report.exact_match_rate:.0%},"
          f"Shadow 日志 {shadow.count()} 条")

    # Gold 回流:关单时从实际执行动作预填,值班人一键接受
    gold_store = GoldStore(tmp)
    suggestion = suggest_from_execution(
        incident_id=result.incident_id, executed_plan=plan,
        top_cause=enr.hypotheses[0].cause_code)
    gold_store.add(accept(suggestion, by="alice", at="2026-07-15T12:30:00Z"))
    print(f"Gold 回流: {gold_store.count()} 条标注")

    # 9. 安全执行网关:L2 审批执行 -> 幂等重放 -> 红色按钮
    step("9. 安全执行网关(F08)")
    from aisre.catalog import AutonomyLevel

    class K8sExecutorStub:
        def dry_run(self, p):
            return True, "server-side dry-run ok"

        def execute(self, p):
            return {"status": "applied", "action_id": p.action_id}

    cat.set_level(key, AutonomyLevel.L2_APPROVAL)   # SHADOW -> L2
    authority = IdentityAuthority(secret="demo-secret")
    gateway = ExecutionGateway(
        catalog=cat, policies=default_policy_set(allowed_namespaces=("payment",)),
        authority=authority,
        executors={"rollback_release": K8sExecutorStub(),
                   "scale_out": K8sExecutorStub()},
        incident_is_open=lambda iid: True, audit_dir=tmp)
    agent_token = authority.issue("ai-sre-orchestrator", "agent",
                                  issued_at=NOW, ttl_seconds=3600)
    alice_token = authority.issue("alice", "human", issued_at=NOW,
                                  ttl_seconds=3600)
    plan.parameters["rollback_to_version"] = "v41"   # 还原演示用参数
    appr2 = approve(plan, approver="alice", approved_at=NOW)
    decision = gateway.execute(plan=plan, cause_code="RECENT_RELEASE_REGRESSION",
                               agent_token=agent_token, now=NOW,
                               approval=appr2, approver_token=alice_token)
    print(f"L2 审批执行: executed={decision.executed},"
          f"检查链 {len(decision.checks)} 环: {decision.checks[:4]}…")
    replay2 = gateway.execute(plan=plan, cause_code="RECENT_RELEASE_REGRESSION",
                              agent_token=agent_token, now=NOW,
                              approval=appr2, approver_token=alice_token)
    print(f"幂等重放: idempotent_replay={replay2.idempotent_replay}")
    gateway.kill(by="alice", at=NOW)
    blocked = gateway.execute(plan=plan, cause_code="RECENT_RELEASE_REGRESSION",
                              agent_token=agent_token, now=NOW,
                              approval=appr2, approver_token=alice_token)
    print(f"红色按钮后: stage={blocked.stage},reason={blocked.reason}")
    gateway.resume(by="alice", at=NOW)
    audit_lines = (Path(tmp) / "gateway_audit.jsonl").read_text(
        encoding="utf-8").splitlines()
    print(f"审计记录: {len(audit_lines)} 条(含 kill/resume 与全部尝试)")

    # 10. Guardian:执行后守护 + 故障注入自动回滚 + 熔断
    step("10. Guardian 执行后守护(F09)")

    class GuardianExecutorStub:
        def __init__(self):
            self.rollbacks = []

        def rollback(self, p):
            self.rollbacks.append(p.action_id)
            return {"status": "compensated", "compensation": p.rollback}

    gex = GuardianExecutorStub()
    healthy = guard(plan, [{"sli_recovered_5m": True,
                            "no_new_error_signature": True}], gex)
    print(f"健康观测: outcome={healthy.outcome}"
          f"(消费 {healthy.observations_consumed} 个观测,无回滚)")
    faulted = guard(plan, [{"regression_signals": ["crashloop"]}], gex,
                    on_rollback=lambda: cat.set_level(key,
                                                      AutonomyLevel.SUSPENDED))
    print(f"故障注入: outcome={faulted.outcome},补偿动作={gex.rollbacks[-1]},"
          f"scope 熔断为 {cat.autonomy_level(key).value}")
    followup = ActionPlan.from_dict({**plan.to_dict(),
                                     "action_id": "act-after",
                                     "idempotency_key": "inc-001-rollback-after"})
    after = gateway.execute(
        plan=followup, cause_code="RECENT_RELEASE_REGRESSION",
        agent_token=agent_token, now=NOW,
        approval=approve(followup, approver="alice", approved_at=NOW),
        approver_token=alice_token)
    print(f"熔断后网关拒绝后续: executed={after.executed},stage={after.stage}")

    # 11. 生产 Shadow:同一 run 只生成计划记入 ledger,绝不执行
    step("11. 生产 Shadow(F11)")
    ledger = ShadowLedger(tmp)
    shadow_rec = shadow_evaluate(
        run, target={"cluster": "prod-cn-east", "namespace": "payment",
                     "workload": "payment-api"},
        expires_at="2026-07-15T10:20:00Z")
    ledger.append(shadow_rec)
    plan_desc = (shadow_rec.plan["action_type"] if shadow_rec.plan
                 else shadow_rec.plan_refusal)
    print(f"Shadow 记录: mode={shadow_rec.mode},计划={plan_desc},"
          f"ledger 累积 {ledger.count()} 例(服务 500 例准入门槛)")

    # 12. 指标看板:全部从记录计算
    step("12. 指标看板(F13)")
    board = build_board(
        enrichment_latencies=[first_publish_latency],   # p95 以首次发布为准,追加不重置
        evidence_coverages=[evidence_coverage(enr)],
        eval_report=eval_report,
        shadow_cases=shadow.count() + ledger.count(),
        real_l2_executions=1,
        gold_labels=gold_store.count(),
        policy_bypasses=0, severe_wrong_actions=0)
    print(f"就绪预览 l3_readiness_preview="
          f"{board['admission']['l3_readiness_preview']},"
          f"授权门禁={board['admission']['authoritative_gate']}")

    # 13. L3 准入门禁:开发完成 ≠ 指标达标(结构性强制)
    step("13. L3 准入门禁(F12,开发完成 != 指标达标)")
    # 可派生字段从本次 demo 的真实工件算出,不手填
    derived = derive_pilot_counts(
        shadow_ledger=ledger, shadow_log=shadow,
        gateway_audit_dir=tmp, eval_report=eval_report)
    print(f"从记录派生: {derived}")
    dev_complete = PilotMetrics(
        **derived,
        pilot_weeks=0.0, valid_incidents=0,      # 没进过试点(人工证词)
        weeks_continuous_compliant=0,
        ai_change_failure_rate=None,             # 没有真实变更数据
        baseline_change_failure_rate=0.05,
        policy_bypasses=0, severe_wrong_actions=0,
        ai_caused_severe_incidents=0, fault_injection_pass_rate=1.0)
    decision = evaluate_l3_admission(dev_complete)
    print(f"数据门: l3_eligible={decision.l3_eligible},"
          f"缺 {len(decision.blocking)} 道: {decision.blocking}")

    # 绕过门禁直升 L3 的路径在 API 上不存在
    try:
        cat.set_level(key, AutonomyLevel.L3_AUTO)
    except ValueError as exc:
        print(f"set_level 直升 L3: 被拒({exc})")
    # 即便找来两个真人批准,数据门未过也无法晋级
    bob_token = authority.issue("bob", "human", issued_at=NOW,
                                ttl_seconds=3600)
    try:
        promote_to_l3(catalog=cat, scope=key, metrics=dev_complete,
                      authority=authority, approver_token_a=alice_token,
                      approver_token_b=bob_token, now=NOW)
    except AdmissionDenied as exc:
        print(f"promote_to_l3(双人批准就位): 仍被拒,卡在 {exc.blocking}")


def _load(name):
    path = Path(__file__).parent / "data" / name
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


if __name__ == "__main__":
    main()
