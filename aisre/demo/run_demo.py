"""端到端演示：服务登记 → 告警丰富 → 动作规划 → 审批绑定 → 90 天基线。

运行：python3 demo/run_demo.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aisre.actions import ActionPlan, approve, is_approval_valid, validate_action_plan
from aisre.baseline import ChangeRecord, IncidentRecord, compute_baseline
from aisre.catalog import ServiceCatalog, ServiceEntry
from aisre.scenarios import get_scenario
from aisre.schemas import Enrichment, Evidence, Fact, Hypothesis, evidence_coverage

NOW = "2026-07-15T10:12:00Z"


def step(title):
    print(f"\n=== {title} ===")


def main():
    # 1. 服务目录：登记试点服务并授予 SHADOW scope
    step("1. 服务目录与自治 scope")
    cat = ServiceCatalog()
    cat.register(ServiceEntry(
        name="payment-api", tier=1, stateless=True, platform="kubernetes",
        cluster="prod-cn-east", namespace="payment", workload="payment-api",
        owners=["team-payment"], slo={"error_rate_pct": 1.0}))
    key = cat.grant_scope("payment-api", "RECENT_RELEASE_REGRESSION",
                          "rollback_release")
    print(f"scope: {key} -> {cat.autonomy_level(key).value}")

    # 2. 告警丰富：证据 → 事实 → Top 假设
    step("2. 告警丰富（事实必须带证据）")
    enr = Enrichment(incident_id="inc-001",
                     alert_received_at="2026-07-15T10:08:00Z")
    enr.add_evidence(Evidence(
        evidence_id="metric-1", source="metrics",
        query="sum(rate(http_errors_total[5m]))",
        time_range=("2026-07-15T10:00:00Z", "2026-07-15T10:10:00Z"),
        url="https://grafana.example.com/d/abc",
        snapshot={"before": 0.002, "after": 0.081}))
    enr.add_evidence(Evidence(
        evidence_id="deploy-42", source="release",
        query="deployments?service=payment-api&limit=1",
        time_range=("2026-07-15T10:05:00Z", "2026-07-15T10:05:00Z"),
        url="https://deploy.example.com/r/42",
        snapshot={"version": "v42", "previous": "v41"}))
    enr.add_fact(Fact(
        fact_id="fact-101",
        text="错误率在 v42 发布后 5 分钟从 0.2% 升至 8.1%",
        observed_at="2026-07-15T10:10:00Z",
        evidence_ids=["metric-1", "deploy-42"]))
    enr.add_hypothesis(Hypothesis(
        rank=1, cause_code="RECENT_RELEASE_REGRESSION",
        evidence_for=["fact-101"], evidence_against=[],
        verification_steps=["compare_canary_baseline"], confidence=0.93))
    print(f"事实 {len(enr.facts)} 条，证据覆盖率 {evidence_coverage(enr):.0%}，"
          f"Top 假设: {enr.hypotheses[0].cause_code}")

    # 3. 动作规划：按场景白名单生成 rollback_release 计划并校验
    step("3. 动作契约校验")
    scenario = get_scenario(enr.hypotheses[0].cause_code)
    plan = ActionPlan(
        action_id="act-20260715-001", incident_id="inc-001",
        action_type="rollback_release", service="payment-api",
        target={"cluster": "prod-cn-east", "namespace": "payment",
                "workload": "payment-api"},
        parameters={"current_version": "v42", "rollback_to_version": "v41"},
        preconditions=["release_correlated", "no_db_schema_change",
                       "artifact_v41_available"],
        success_criteria=["sli_recovered_5m", "no_new_error_signature"],
        rollback={"action_type": "redeploy_version", "version": "v42"},
        idempotency_key="inc-001-rollback-v1",
        expires_at="2026-07-15T10:20:00Z")
    violations = validate_action_plan(plan, now=NOW, scenario=scenario)
    print(f"违规: {violations or '无'}，plan_hash: {plan.plan_hash()[:16]}…")

    # 4. 审批绑定 plan_hash：参数变化后审批失效
    step("4. 审批绑定")
    appr = approve(plan, approver="alice", approved_at=NOW)
    print(f"原计划审批有效: {is_approval_valid(plan, appr)}")
    plan.parameters["rollback_to_version"] = "v40"
    print(f"参数被改后审批有效: {is_approval_valid(plan, appr)}")

    # 5. 90 天基线
    step("5. 90 天基线")
    incidents = [IncidentRecord(**d) for d in _load("incidents.jsonl")]
    changes = [ChangeRecord(**d) for d in _load("changes.jsonl")]
    report = compute_baseline(incidents=incidents, changes=changes,
                              as_of="2026-07-15T00:00:00Z")
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))


def _load(name):
    path = Path(__file__).parent / "data" / name
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


if __name__ == "__main__":
    main()
