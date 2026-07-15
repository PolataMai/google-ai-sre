"""MVP 三类诊断场景的封闭注册表。

场景 = cause_code + 检测信号 + 验证步骤 + 允许动作。
allowed_actions 是场景级白名单：动作规划时 ActionPlan.action_type
必须落在所属场景的 allowed_actions 内（由 actions.validate_action_plan 强制）。
单实例异常在 MVP 阶段只调查不动作（重启实例不在两个可逆 L2 动作之列）。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class CauseCode(str, Enum):
    RECENT_RELEASE_REGRESSION = "RECENT_RELEASE_REGRESSION"
    CAPACITY_SATURATION = "CAPACITY_SATURATION"
    SINGLE_INSTANCE_ANOMALY = "SINGLE_INSTANCE_ANOMALY"


class UnknownScenario(KeyError):
    pass


@dataclass(frozen=True)
class ScenarioDef:
    cause_code: CauseCode
    title: str
    detection_signals: tuple[str, ...]   # 判定该场景成立所需的信号种类
    verification_steps: tuple[str, ...]  # 假设的验证步骤（供 Hypothesis 引用）
    allowed_actions: tuple[str, ...]     # 允许的 L2 动作类型白名单


_SCENARIOS: dict[CauseCode, ScenarioDef] = {
    CauseCode.RECENT_RELEASE_REGRESSION: ScenarioDef(
        cause_code=CauseCode.RECENT_RELEASE_REGRESSION,
        title="最近发布引发错误率或延迟上升",
        detection_signals=(
            "deploy_event_within_window",   # 时间窗内存在发布事件
            "error_rate_rise_after_deploy",  # 发布后错误率上升
            "latency_rise_after_deploy",     # 或发布后延迟上升
        ),
        verification_steps=(
            "compare_canary_baseline",       # 新旧版本实例指标对比
            "diff_release_changelog",        # 检查变更内容与故障面交集
            "check_error_signature_novelty",  # 错误特征是否随发布首次出现
        ),
        allowed_actions=("rollback_release",),
    ),
    CauseCode.CAPACITY_SATURATION: ScenarioDef(
        cause_code=CauseCode.CAPACITY_SATURATION,
        title="CPU/内存/连接池或副本容量不足",
        detection_signals=(
            "cpu_or_memory_saturation",
            "connection_pool_exhaustion",
            "replica_utilization_high",
        ),
        verification_steps=(
            "compare_load_vs_capacity",      # 负载曲线与容量水位对比
            "check_hpa_and_quota_status",    # HPA/配额状态
            "confirm_no_recent_deploy",      # 排除发布回归（与场景一互斥验证）
        ),
        allowed_actions=("scale_out",),
    ),
    CauseCode.SINGLE_INSTANCE_ANOMALY: ScenarioDef(
        cause_code=CauseCode.SINGLE_INSTANCE_ANOMALY,
        title="单实例行为异常、其他实例正常",
        detection_signals=(
            "one_instance_deviates",         # 单实例指标显著偏离
            "peer_instances_healthy",
        ),
        verification_steps=(
            "compare_instance_percentiles",  # 实例间分位数对比
            "check_node_events",             # 宿主节点事件（驱逐、OOM、硬件）
            "inspect_instance_logs",
        ),
        allowed_actions=(),  # MVP 只调查不动作
    ),
}


def list_scenarios() -> list[ScenarioDef]:
    return list(_SCENARIOS.values())


def get_scenario(code: CauseCode | str) -> ScenarioDef:
    try:
        return _SCENARIOS[CauseCode(code)]
    except ValueError:
        raise UnknownScenario(code) from None
