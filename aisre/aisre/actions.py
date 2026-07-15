"""动作契约（F06 标准动作目录 / F07 动作规划的 Schema 层）。

约束：
- 封闭动作目录：只有 scale_out / rollback_release，禁止任意脚本；
- scale_out 扩容幅度 10%–25%（下限向上取整、上限向下取整，至少 +1 副本），
  rollback 必须恢复 original_replicas；
- 每个计划自带 TTL 与强制 dry_run；
- 审批绑定 action_id + plan_hash：参数任何变化都会改变哈希，原审批立即失效；
- 传入场景时，动作类型必须落在场景 allowed_actions 白名单内。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Union

from aisre.scenarios import ScenarioDef

ALLOWED_ACTION_TYPES = ("scale_out", "rollback_release")

_NUMERIC_OPS = {
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
}
_CRITERION_RE = re.compile(r"^(\w+)\s*(<=|>=|<|>)\s*([0-9]+(?:\.[0-9]+)?)(%?)$")
_FLAG_RE = re.compile(r"^\w+$")


@dataclass(frozen=True)
class SuccessCriterion:
    """结构化成功条件(契约层):数值比较 {metric, op, threshold},
    或命名布尔 {metric, op="is_true", threshold=None}。

    构造即校验:op 与 threshold 不匹配当场抛 ValueError;非法输入不会
    悄悄存活到 Guardian 变成"永久未决"。
    """
    metric: str
    op: str                            # < <= > >= 或 is_true
    threshold: Optional[float] = None

    def __post_init__(self):
        if self.op == "is_true":
            if self.threshold is not None:
                raise ValueError(f"布尔条件 {self.metric} 不应带 threshold")
        elif self.op in _NUMERIC_OPS:
            if not isinstance(self.threshold, (int, float)) \
                    or isinstance(self.threshold, bool):
                raise ValueError(
                    f"数值条件 {self.metric}{self.op} 需要数值 threshold,"
                    f"当前 {self.threshold!r}")
        else:
            raise ValueError(f"未知比较符 {self.op!r}(仅支持 "
                             f"{sorted(_NUMERIC_OPS)} 或 is_true)")

    @classmethod
    def parse(cls, s: str) -> "SuccessCriterion":
        """从人类简写解析:"error_rate<1%" / "sli_recovered_5m"。
        非法格式抛 ValueError(响亮失败,不静默)。"""
        text = (s or "").strip()
        m = _CRITERION_RE.match(text)
        if m:
            metric, op, num, pct = m.groups()
            threshold = float(num) / 100 if pct else float(num)
            return cls(metric=metric, op=op, threshold=threshold)
        if _FLAG_RE.match(text):
            return cls(metric=text, op="is_true", threshold=None)
        raise ValueError(f"无法解析成功条件: {s!r}")

    def evaluate(self, observation: dict) -> Optional[bool]:
        """返回 True/False;仅当指标在观测中缺失时返回 None(合法未决)。"""
        if self.op == "is_true":
            if self.metric not in observation:
                return None
            return bool(observation[self.metric])
        observed = observation.get(self.metric)
        if not isinstance(observed, (int, float)) or isinstance(observed, bool):
            return None
        return _NUMERIC_OPS[self.op](observed, self.threshold)

    def to_dict(self) -> dict:
        return {"metric": self.metric, "op": self.op,
                "threshold": self.threshold}

    @staticmethod
    def from_dict(d: dict) -> "SuccessCriterion":
        return SuccessCriterion(metric=d["metric"], op=d["op"],
                                threshold=d.get("threshold"))

    def __str__(self) -> str:
        if self.op == "is_true":
            return self.metric
        return f"{self.metric}{self.op}{self.threshold}"


CriterionInput = Union["SuccessCriterion", dict, str]


def _coerce_criterion(c: CriterionInput) -> SuccessCriterion:
    if isinstance(c, SuccessCriterion):
        return c
    if isinstance(c, dict):
        return SuccessCriterion.from_dict(c)
    if isinstance(c, str):
        return SuccessCriterion.parse(c)
    raise TypeError(f"success_criteria 元素类型非法: {type(c)}")


def _parse_ts(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass
class ActionPlan:
    action_id: str
    incident_id: str
    action_type: str
    service: str
    target: dict                      # cluster / namespace / workload
    parameters: dict
    preconditions: list[str]
    success_criteria: list                # 归一为 list[SuccessCriterion]
    rollback: dict                    # 补偿动作定义
    idempotency_key: str
    expires_at: str
    dry_run_required: bool = True

    def __post_init__(self):
        # 契约层归一:字符串/字典/对象一律转成 SuccessCriterion,
        # 非法格式在此当场抛错(不留到 Guardian 变成静默超时回滚)。
        self.success_criteria = [_coerce_criterion(c)
                                 for c in self.success_criteria]

    def to_dict(self) -> dict:
        return {
            "action_id": self.action_id,
            "incident_id": self.incident_id,
            "action_type": self.action_type,
            "service": self.service,
            "target": dict(self.target),
            "parameters": dict(self.parameters),
            "preconditions": list(self.preconditions),
            "success_criteria": [c.to_dict() for c in self.success_criteria],
            "rollback": dict(self.rollback),
            "idempotency_key": self.idempotency_key,
            "expires_at": self.expires_at,
            "dry_run_required": self.dry_run_required,
        }

    @staticmethod
    def from_dict(d: dict) -> "ActionPlan":
        return ActionPlan(
            action_id=d["action_id"],
            incident_id=d["incident_id"],
            action_type=d["action_type"],
            service=d["service"],
            target=dict(d["target"]),
            parameters=dict(d["parameters"]),
            preconditions=list(d["preconditions"]),
            success_criteria=list(d["success_criteria"]),
            rollback=dict(d["rollback"]),
            idempotency_key=d["idempotency_key"],
            expires_at=d["expires_at"],
            dry_run_required=d.get("dry_run_required", True),
        )

    def plan_hash(self) -> str:
        """对全部字段做规范化 JSON 哈希——任何字段变化都会改变哈希。"""
        canonical = json.dumps(self.to_dict(), sort_keys=True,
                               ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


def _scale_out_bounds(original: int) -> tuple[int, int]:
    """允许的副本增量区间 [min, max]：10% 向上取整（至少 1），25% 向下取整。"""
    lo = max(1, math.ceil(original * 0.10))
    hi = math.floor(original * 0.25)
    return lo, hi


def validate_action_plan(plan: ActionPlan, now: str,
                         scenario: Optional[ScenarioDef] = None) -> list[str]:
    """返回违规清单；空列表 = 通过。now 由调用方注入，便于测试与回放。"""
    violations: list[str] = []

    if plan.action_type not in ALLOWED_ACTION_TYPES:
        violations.append(
            f"action_type {plan.action_type} 不在标准动作目录 {ALLOWED_ACTION_TYPES}")
        return violations  # 未知动作不再做类型专属校验

    if _parse_ts(plan.expires_at) <= _parse_ts(now):
        violations.append(f"计划已过期: expires_at={plan.expires_at} <= now={now}")
    if not plan.dry_run_required:
        violations.append("dry_run_required 必须为 True")
    if not plan.preconditions:
        violations.append("preconditions 不能为空")
    if not plan.success_criteria:
        violations.append("success_criteria 不能为空")
    if not plan.rollback:
        violations.append("必须定义 rollback 补偿动作")

    if plan.action_type == "scale_out":
        original = plan.parameters.get("original_replicas")
        target = plan.parameters.get("target_replicas")
        if not isinstance(original, int) or not isinstance(target, int):
            violations.append("scale_out 需要整数 original_replicas / target_replicas")
        else:
            lo, hi = _scale_out_bounds(original)
            incr = target - original
            if incr > hi:
                violations.append(
                    f"扩容 +{incr} 超过 25% 上限（最多 +{hi}）")
            elif incr < lo:
                violations.append(
                    f"扩容 +{incr} 低于 10% 下限（至少 +{lo}）")
            if plan.rollback.get("target_replicas") != original:
                violations.append(
                    f"rollback 必须恢复原副本数 {original}，"
                    f"当前为 {plan.rollback.get('target_replicas')}")

    if plan.action_type == "rollback_release":
        for key in ("current_version", "rollback_to_version"):
            if not plan.parameters.get(key):
                violations.append(f"rollback_release 缺少参数 {key}")

    if scenario is not None and plan.action_type not in scenario.allowed_actions:
        violations.append(
            f"动作 {plan.action_type} 不在场景 {scenario.cause_code.value} "
            f"的白名单 {scenario.allowed_actions} 内")

    return violations


@dataclass
class Approval:
    action_id: str
    plan_hash: str
    approver: str
    approved_at: str


def approve(plan: ActionPlan, approver: str, approved_at: str) -> Approval:
    return Approval(action_id=plan.action_id, plan_hash=plan.plan_hash(),
                    approver=approver, approved_at=approved_at)


def is_approval_valid(plan: ActionPlan, approval: Approval) -> bool:
    """审批绑定 action_id + plan_hash：参数变化后原审批立即失效。"""
    return (approval.action_id == plan.action_id
            and approval.plan_hash == plan.plan_hash())
