"""Guardian(F09):执行后 SLI 守护与自动回滚。

对应安全闭环的 `执行 → Guardian → 审计`:动作落地后 Guardian 接管观测,
按成功条件裁决,失败自动执行补偿动作并熔断该服务后续自动化。

观测序列模型:一次 guard() 消费该窗口内的全部观测快照(由观测源按固定
间隔采集,携带 SLI 指标与布尔标志)。裁决优先级:
  1. 恶化信号(regression_signals 非空)→ 立即回滚(不等窗口结束);
  2. 全部 success_criteria 达成 → 放行;
  3. 窗口内始终无法确认成功 → 保守回滚(fail closed:拿不到 SLI 也止血)。

成功条件求值(evaluate_criterion):
  - 数值比较 "metric<value" / "metric<value%":观测取小数,% 阈值 /100;
  - 命名布尔 "flag":观测同名键的真值;
  - 缺指标返回 None(未决,继续等)——None 永远不算成功。

executor 只需 rollback(plan) -> dict(读 plan.rollback 执行补偿);
on_rollback 是熔断回调(生产接线到 catalog.set_level(scope, SUSPENDED))。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional

_NUMERIC = re.compile(r"^(\w+)\s*([<>]=?)\s*([\d.]+)(%?)$")
_OPS = {
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
}


def evaluate_criterion(criterion: str, observation: dict) -> Optional[bool]:
    """返回 True/False,或 None 表示当前观测无法判定该条件。"""
    m = _NUMERIC.match(criterion)
    if m:
        metric, op, value, pct = m.groups()
        observed = observation.get(metric)
        if not isinstance(observed, (int, float)) or isinstance(observed, bool):
            return None
        threshold = float(value) / 100 if pct else float(value)
        return _OPS[op](observed, threshold)
    if criterion in observation:
        return bool(observation[criterion])
    return None


@dataclass
class GuardianVerdict:
    outcome: str                       # success / rolled_back
    reason: str
    rollback_result: Optional[dict]
    observations_consumed: int
    rolled_back: bool


def _rollback(plan, executor, on_rollback, reason: str,
              consumed: int) -> GuardianVerdict:
    result = executor.rollback(plan)
    if on_rollback is not None:
        on_rollback()                  # 熔断:停止该服务后续自动动作,升级人工
    return GuardianVerdict(outcome="rolled_back", reason=reason,
                           rollback_result=result,
                           observations_consumed=consumed, rolled_back=True)


def guard(plan, observations: list[dict], executor, *,
          on_rollback: Optional[Callable[[], None]] = None) -> GuardianVerdict:
    for i, obs in enumerate(observations, start=1):
        signals = obs.get("regression_signals") or []
        if signals:
            return _rollback(plan, executor, on_rollback,
                             f"检测到恶化信号 {signals},立即回滚", i)
        verdicts = [evaluate_criterion(c, obs) for c in plan.success_criteria]
        if verdicts and all(v is True for v in verdicts):
            return GuardianVerdict(
                outcome="success",
                reason=f"全部成功条件达成: {list(plan.success_criteria)}",
                rollback_result=None, observations_consumed=i,
                rolled_back=False)
    return _rollback(plan, executor, on_rollback,
                     "观测窗口内未确认成功条件,保守回滚(fail closed)",
                     len(observations))
