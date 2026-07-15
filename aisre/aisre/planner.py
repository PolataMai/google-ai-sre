"""Shadow 计划器(F07 的雏形):Top-1 假设 → 类型化 ActionPlan 草案。

只生成、不执行(Shadow 语义)。原则:
- 置信门槛:Top-1 置信度 < 0.8 不生成(低置信下生成计划就是猜);
- 参数只取自事实:回滚版本来自发布事实 meta,副本数来自 metrics 快照
  current_replicas——缺参数直接拒绝并给出机器可读原因,不猜;
- 扩容取 +15%(10–25% 边界的中间值,ceil 保证至少 +1);
- 产出必须能通过 validate_action_plan(含场景白名单)——
  计划器和校验器互为制衡,同错的概率远小于单点。
"""
from __future__ import annotations

import math
from typing import Optional

from aisre.actions import ActionPlan
from aisre.enrichment import EnrichmentRun
from aisre.scenarios import get_scenario

CONFIDENCE_FLOOR = 0.8
SCALE_STEP = 0.15


def draft_plan(run: EnrichmentRun, target: dict,
               expires_at: str) -> tuple[Optional[ActionPlan], Optional[str]]:
    """返回 (plan, None) 或 (None, 机器可读拒绝原因)。"""
    top = run.enrichment.hypotheses[0]
    if top.confidence < CONFIDENCE_FLOOR:
        return None, "low_confidence"
    scenario = get_scenario(top.cause_code)
    if not scenario.allowed_actions:
        return None, "investigate_only"

    incident_id = run.enrichment.incident_id
    action_type = scenario.allowed_actions[0]

    if action_type == "rollback_release":
        deploys = [f for f in run.extracted if f.kind == "recent_deploy"]
        if not deploys or not deploys[0].meta.get("previous"):
            return None, "missing_previous_version"
        meta = deploys[0].meta
        plan = ActionPlan(
            action_id=f"act-{incident_id}-rollback",
            incident_id=incident_id,
            action_type="rollback_release",
            service=run.service,
            target=dict(target),
            parameters={"current_version": meta["version"],
                        "rollback_to_version": meta["previous"]},
            preconditions=["release_correlated", "no_db_schema_change",
                           f"artifact_{meta['previous']}_available"],
            success_criteria=["sli_recovered_5m", "no_new_error_signature"],
            rollback={"action_type": "redeploy_version",
                      "version": meta["version"]},
            idempotency_key=f"{incident_id}-rollback-v1",
            expires_at=expires_at)
        return plan, None

    if action_type == "scale_out":
        current = _current_replicas(run)
        if current is None:
            return None, "missing_current_replicas"
        increase = max(1, math.ceil(current * SCALE_STEP))
        plan = ActionPlan(
            action_id=f"act-{incident_id}-scale",
            incident_id=incident_id,
            action_type="scale_out",
            service=run.service,
            target=dict(target),
            parameters={"original_replicas": current,
                        "target_replicas": current + increase},
            preconditions=["quota_available", "no_active_rollout",
                           f"current_replicas={current}"],
            success_criteria=["error_rate<1%", "slo_burn_rate<2"],
            rollback={"action_type": "restore_replicas",
                      "target_replicas": current},
            idempotency_key=f"{incident_id}-scale-out-v1",
            expires_at=expires_at)
        return plan, None

    return None, f"no_planner_for_{action_type}"


def _current_replicas(run: EnrichmentRun) -> Optional[int]:
    for ev in run.enrichment.evidences.values():
        if ev.source == "metrics" and isinstance(ev.snapshot, dict):
            value = ev.snapshot.get("current_replicas")
            if isinstance(value, int):
                return value
    return None
