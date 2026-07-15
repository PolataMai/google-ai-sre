"""Guardian(F09):执行后 SLI 守护与自动回滚。

对应安全闭环的 `执行 → Guardian → 审计`:动作落地后 Guardian 接管观测,
按成功条件裁决,失败自动执行补偿动作并熔断该服务后续自动化。

观测序列模型:一次 guard() 消费该窗口内的全部观测快照(由观测源按固定
间隔采集,携带 SLI 指标与布尔标志)。裁决优先级:
  1. 恶化信号(regression_signals 非空)→ 立即回滚(不等窗口结束);
  2. 全部 success_criteria 达成 → 放行;
  3. 窗口内始终无法确认成功 → 保守回滚(fail closed:拿不到 SLI 也止血)。

成功条件求值:直接调用契约层 SuccessCriterion.evaluate(观测)——
Guardian 不再自己解析字符串,成功条件的结构与合法性由 actions.py 契约层
在 ActionPlan 构造时就保证;这里只可能因"指标缺失"得到 None(合法未决),
不会因"格式非法"静默变成永久未决。

executor 只需 rollback(plan) -> dict(读 plan.rollback 执行补偿);
on_rollback 是熔断回调(生产接线到 catalog.set_level(scope, SUSPENDED))。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional


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
        verdicts = [c.evaluate(obs) for c in plan.success_criteria]
        if verdicts and all(v is True for v in verdicts):
            return GuardianVerdict(
                outcome="success",
                reason="全部成功条件达成: "
                       + ", ".join(str(c) for c in plan.success_criteria),
                rollback_result=None, observations_consumed=i,
                rolled_back=False)
    return _rollback(plan, executor, on_rollback,
                     "观测窗口内未确认成功条件,保守回滚(fail closed)",
                     len(observations))
