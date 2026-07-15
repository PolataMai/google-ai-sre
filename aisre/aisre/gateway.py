"""安全执行网关(F08):所有生产写操作的唯一通道。

对应文章的 Actus 模式:推理引擎与执行引擎解耦——本模块不 import 任何
推理侧代码(connectors/enrichment/hypotheses/planner),只认签名有效、
未过期、通过契约校验的 ActionPlan;模型无论怎么进化,变更生产的能力
始终被这条确定性检查链约束:

  红色按钮 → Agent 身份 → 动作契约(Schema/TTL/场景白名单)→ 幂等
  → 事故仍有效 → 自治 scope → 并发锁 → 限流 → Dry-run → 策略
  → L2 审批 / L3 资格 → 执行 → 审计

性质:默认拒绝;任何依赖抛异常按拒绝处理(fail closed),不冒泡;
提交者必须是 agent 主体,审批人必须是 human 主体;每次尝试(含拒绝)
都追加审计记录。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from aisre.actions import (ActionPlan, Approval, is_approval_valid,
                           validate_action_plan)
from aisre.catalog import AutonomyLevel, ServiceCatalog, scope_key
from aisre.identity import IdentityAuthority, InvalidToken
from aisre.policy import PolicySet, evaluate
from aisre.scenarios import UnknownScenario, get_scenario


def _parse_ts(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class _Denied(Exception):
    def __init__(self, stage: str, reason: str):
        super().__init__(reason)
        self.stage = stage
        self.reason = reason


@dataclass
class ExecutionDecision:
    allowed: bool
    executed: bool
    stage: str                     # 失败环节名,或 "executed"
    reason: Optional[str] = None
    result: Optional[dict] = None
    idempotent_replay: bool = False
    policy_version: Optional[str] = None
    checks: list[str] = field(default_factory=list)


class ExecutionGateway:
    def __init__(self, *, catalog: ServiceCatalog, policies: PolicySet,
                 authority: IdentityAuthority, executors: dict,
                 incident_is_open: Callable[[str], bool],
                 audit_dir: str, max_executions_per_hour: int = 10):
        self._catalog = catalog
        self._policies = policies
        self._authority = authority
        self._executors = executors
        self._incident_is_open = incident_is_open
        self._audit_path = Path(audit_dir) / "gateway_audit.jsonl"
        self._audit_path.parent.mkdir(parents=True, exist_ok=True)
        self._max_per_hour = max_executions_per_hour
        self._killed = False
        self._executed: dict[str, ExecutionDecision] = {}   # idempotency_key →
        self._active: dict[str, str] = {}                   # service → action_id
        self._history: list[tuple[str, datetime]] = []      # (service, at)

    # ---- 红色按钮 ----

    def kill(self, by: str, at: str) -> None:
        self._killed = True
        self._audit_event("red_button_control", {"op": "kill", "by": by,
                                                 "at": at})

    def resume(self, by: str, at: str) -> None:
        self._killed = False
        self._audit_event("red_button_control", {"op": "resume", "by": by,
                                                 "at": at})

    # ---- 执行 ----

    def execute(self, *, plan: ActionPlan, cause_code: str, agent_token: str,
                now: str, approval: Optional[Approval] = None,
                approver_token: Optional[str] = None) -> ExecutionDecision:
        checks: list[str] = []
        principal_id: Optional[str] = None
        policy_version: Optional[str] = None
        try:
            # 1. 红色按钮:计划评估前检查一次
            if self._killed:
                raise _Denied("red_button", "Kill Switch 已触发,拒绝一切自动化")
            checks.append("red_button")

            # 2. Agent 独立身份
            try:
                principal = self._authority.verify(agent_token, now=now)
            except InvalidToken as exc:
                raise _Denied("identity", f"身份验证失败: {exc}") from None
            if principal.principal_type != "agent":
                raise _Denied("identity",
                              f"提交者 {principal.principal_id} 不是 agent 主体"
                              f"——人工运维走原有链路,不经网关")
            principal_id = principal.principal_id
            checks.append("identity")

            # 3. 动作契约:Schema + TTL + 场景白名单
            try:
                scenario = get_scenario(cause_code)
            except UnknownScenario:
                raise _Denied("contract", f"未知场景 {cause_code}") from None
            violations = validate_action_plan(plan, now=now, scenario=scenario)
            if violations:
                raise _Denied("contract", "; ".join(violations))
            checks.append("contract")

            # 4. 幂等:重复请求返回上次结果,不二次执行
            if plan.idempotency_key in self._executed:
                prior = self._executed[plan.idempotency_key]
                decision = ExecutionDecision(
                    allowed=True, executed=True, stage="executed",
                    result=prior.result, idempotent_replay=True,
                    checks=checks + ["idempotency_replay"])
                self._audit(plan, principal_id, decision, now)
                return decision
            checks.append("idempotency")

            # 5. 事故仍然有效
            if not self._incident_is_open(plan.incident_id):
                raise _Denied("incident_open",
                              f"事故 {plan.incident_id} 已关闭,动作作废")
            checks.append("incident_open")

            # 6. 自治 scope:服务×场景×动作×环境
            key = scope_key(plan.service, cause_code, plan.action_type,
                            plan.target.get("cluster", ""))
            level = self._catalog.autonomy_level(key)
            if level not in (AutonomyLevel.L2_APPROVAL, AutonomyLevel.L3_AUTO):
                raise _Denied("autonomy",
                              f"scope {key} 等级 {level.value if level else '未授权'}"
                              f",无执行权限")
            checks.append(f"autonomy:{level.value}")

            # 7. 并发锁:同服务同时只允许一个在途动作
            active = self._active.get(plan.service)
            if active:
                raise _Denied("concurrency",
                              f"服务 {plan.service} 已有在途动作 {active}")
            checks.append("concurrency")

            # 8. 限流(Agent 级熔断)
            window_start = _parse_ts(now) - timedelta(hours=1)
            recent = sum(1 for svc, at in self._history
                         if svc == plan.service and at > window_start)
            if recent >= self._max_per_hour:
                raise _Denied("rate_limit",
                              f"服务 {plan.service} 1 小时内已执行 {recent} 次"
                              f",达到上限 {self._max_per_hour}")
            checks.append("rate_limit")

            # 9. 强制 Dry-run(fail closed:适配器异常按失败处理)
            executor = self._executors.get(plan.action_type)
            if executor is None:
                raise _Denied("dry_run", f"无 {plan.action_type} 执行适配器")
            try:
                ok, detail = executor.dry_run(plan)
            except Exception as exc:   # noqa: BLE001
                raise _Denied("dry_run", f"dry-run 异常,fail closed: {exc}") \
                    from None
            if not ok:
                raise _Denied("dry_run", f"dry-run 未通过: {detail}")
            checks.append("dry_run")

            # 10. 策略(OPA 语义:默认拒绝)
            decision = evaluate(self._policies, {
                "action_type": plan.action_type, "service": plan.service,
                "target": plan.target, "parameters": plan.parameters})
            policy_version = decision.policy_version
            if not decision.allow:
                raise _Denied("policy", "; ".join(decision.reasons))
            checks.append(f"policy:{policy_version}")

            # 11. L2 审批 / L3 资格
            if level == AutonomyLevel.L2_APPROVAL:
                self._require_valid_approval(plan, approval, approver_token,
                                             now)
                checks.append("authorization:L2_APPROVAL")
            else:
                checks.append("authorization:L3_AUTO")

            # 12. 执行(fail closed)
            try:
                result = executor.execute(plan)
            except Exception as exc:   # noqa: BLE001
                raise _Denied("execute", f"执行异常: {exc}") from None
            self._active[plan.service] = plan.action_id
            self._history.append((plan.service, _parse_ts(now)))

            final = ExecutionDecision(
                allowed=True, executed=True, stage="executed", result=result,
                policy_version=policy_version, checks=checks + ["executed"])
            self._executed[plan.idempotency_key] = final
            self._audit(plan, principal_id, final, now)
            return final

        except _Denied as denied:
            final = ExecutionDecision(
                allowed=False, executed=False, stage=denied.stage,
                reason=denied.reason, policy_version=policy_version,
                checks=checks)
            self._audit(plan, principal_id, final, now)
            return final

    def _require_valid_approval(self, plan: ActionPlan,
                                approval: Optional[Approval],
                                approver_token: Optional[str],
                                now: str) -> None:
        if approval is None or approver_token is None:
            raise _Denied("authorization",
                          "L2_APPROVAL scope 需要人工审批(approval + 审批人令牌)")
        if not is_approval_valid(plan, approval):
            raise _Denied("authorization",
                          "审批与计划不匹配(参数变化后原审批失效)")
        try:
            approver = self._authority.verify(approver_token, now=now)
        except InvalidToken as exc:
            raise _Denied("authorization", f"审批人身份无效: {exc}") from None
        if approver.principal_type != "human":
            raise _Denied("authorization",
                          f"审批人 {approver.principal_id} 不是人类主体")
        if approver.principal_id != approval.approver:
            raise _Denied("authorization",
                          f"审批人令牌 {approver.principal_id} 与审批记录 "
                          f"{approval.approver} 不一致")

    # ---- LRO 与审计 ----

    def mark_completed(self, action_id: str) -> None:
        for service, active_id in list(self._active.items()):
            if active_id == action_id:
                del self._active[service]

    def _audit(self, plan: ActionPlan, principal_id: Optional[str],
               decision: ExecutionDecision, now: str) -> None:
        self._audit_event("execution_attempt", {
            "at": now,
            "action_id": plan.action_id,
            "plan_hash": plan.plan_hash(),
            "principal": principal_id,
            "stage": decision.stage,
            "allowed": decision.allowed,
            "executed": decision.executed,
            "reason": decision.reason,
            "policy_version": decision.policy_version,
            "idempotent_replay": decision.idempotent_replay,
            "checks": decision.checks,
        })

    def _audit_event(self, kind: str, payload: dict) -> None:
        record = {"kind": kind, **payload}
        with self._audit_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
