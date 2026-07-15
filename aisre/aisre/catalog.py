"""服务目录与自治权限粒度（F12 的数据基础，第 1–2 周先落 Schema 与准入约束）。

- 试点准入：只收 Kubernetes 上的 Tier-1 无状态服务（MVP 范围硬约束）；
- 自治权限的最小粒度是 服务+场景+动作+环境 的 scope，禁止全局 L3；
- 新授予的 scope 一律从 SHADOW 起步——渐进授权的起点；
  晋级状态机（SHADOW → L2_APPROVAL → L3_AUTO / SUSPENDED）在后续迭代实现。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from aisre.scenarios import get_scenario


class PilotEligibilityError(ValueError):
    """服务不满足试点准入条件。"""


class AutonomyLevel(str, Enum):
    SHADOW = "SHADOW"            # 只生成计划，不执行
    L2_APPROVAL = "L2_APPROVAL"  # 可执行，需人工审批
    L3_AUTO = "L3_AUTO"          # 限定场景自动执行
    SUSPENDED = "SUSPENDED"      # 暂停一切自动化


@dataclass
class ServiceEntry:
    name: str
    tier: int
    stateless: bool
    platform: str                # 试点只允许 kubernetes
    cluster: str
    namespace: str
    workload: str
    owners: list[str] = field(default_factory=list)
    slo: dict = field(default_factory=dict)   # 例：{"error_rate_pct": 1.0, "latency_p99_ms": 300}


def scope_key(service: str, cause_code: str, action_type: str,
              environment: str) -> str:
    """自治权限粒度：服务+场景+动作+环境，四段用 + 连接。"""
    return f"{service}+{cause_code}+{action_type}+{environment}"


class ServiceCatalog:
    def __init__(self) -> None:
        self._services: dict[str, ServiceEntry] = {}
        self._scopes: dict[str, AutonomyLevel] = {}

    def register(self, entry: ServiceEntry) -> None:
        if entry.name in self._services:
            raise ValueError(f"服务已登记: {entry.name}")
        problems = []
        if entry.tier != 1:
            problems.append(f"tier={entry.tier}（试点只收 Tier-1）")
        if not entry.stateless:
            problems.append("有状态服务（试点只收无状态）")
        if entry.platform != "kubernetes":
            problems.append(f"platform={entry.platform}（试点只收 kubernetes）")
        if problems:
            raise PilotEligibilityError(f"{entry.name} 不满足试点准入: {problems}")
        self._services[entry.name] = entry

    def get(self, name: str) -> ServiceEntry:
        return self._services[name]

    def grant_scope(self, service: str, cause_code: str,
                    action_type: str) -> str:
        """为已登记服务授予一个自治 scope，从 SHADOW 起步，返回 scope key。"""
        entry = self._services[service]  # 未登记服务直接 KeyError
        scenario = get_scenario(cause_code)
        if action_type not in scenario.allowed_actions:
            raise ValueError(
                f"动作 {action_type} 不在场景 {cause_code} 的白名单 "
                f"{scenario.allowed_actions} 内")
        key = scope_key(service, cause_code, action_type, entry.cluster)
        self._scopes[key] = AutonomyLevel.SHADOW
        return key

    def autonomy_level(self, key: str) -> Optional[AutonomyLevel]:
        """未登记的 scope 返回 None = 无任何自治权限（默认拒绝）。"""
        return self._scopes.get(key)
