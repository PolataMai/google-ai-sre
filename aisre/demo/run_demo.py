"""端到端演示:告警接入 → 并行采集 → 证据库 → 告警丰富 → 动作规划 → 审批 → 基线。

运行:python3 demo/run_demo.py
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aisre.actions import ActionPlan, approve, is_approval_valid, validate_action_plan
from aisre.baseline import ChangeRecord, IncidentRecord, compute_baseline
from aisre.catalog import ServiceCatalog, ServiceEntry
from aisre.connectors import default_connectors
from aisre.enrichment import (enrichment_latency_seconds, refresh_missing,
                              run_enrichment)
from aisre.evidence_store import EvidenceStore
from aisre.intake import IntakeService
from aisre.scenarios import get_scenario
from aisre.schemas import evidence_coverage
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
    print(f"部分发布: partial={run.partial},缺失源: {run.missing_sources},"
          f"守门违规: {run.violations or '无'}")
    print(f"告警→发布延迟: {enrichment_latency_seconds(run.enrichment):.0f}s,"
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
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))


def _load(name):
    path = Path(__file__).parent / "data" / name
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


if __name__ == "__main__":
    main()
