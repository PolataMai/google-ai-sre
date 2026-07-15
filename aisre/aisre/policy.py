"""策略引擎(OPA 替身):策略判断与程序执行分离。

语义与 OPA 对齐,便于后续平移到真 OPA/Rego:
- 输入是结构化 dict,输出 PolicyDecision {allow, reasons, policy_version};
- 默认拒绝:必须至少有一条 allow 规则命中且无任何 deny,才放行;
  空规则集/无匹配一律 deny(fail closed);
- 规则是数据(名称 + 判定函数),决策带策略版本供审计记录。

规则返回值约定:("allow", None) 显式放行票;(None, None) 弃权;
("deny", reason) 一票否决。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Optional

RuleResult = tuple[Optional[str], Optional[str]]   # (verdict, reason)


@dataclass
class PolicyRule:
    name: str
    check: Callable[[dict], RuleResult]


@dataclass
class PolicySet:
    version: str
    rules: list[PolicyRule] = field(default_factory=list)


@dataclass
class PolicyDecision:
    allow: bool
    reasons: list[str]
    policy_version: str


def evaluate(policies: PolicySet, action_input: dict) -> PolicyDecision:
    allows = 0
    denies: list[str] = []
    for rule in policies.rules:
        verdict, reason = rule.check(action_input)
        if verdict == "deny":
            denies.append(f"[{rule.name}] {reason}")
        elif verdict == "allow":
            allows += 1
    if denies:
        return PolicyDecision(False, denies, policies.version)
    if allows == 0:
        return PolicyDecision(False, ["默认拒绝:无允许规则命中"],
                              policies.version)
    return PolicyDecision(True, [], policies.version)


def default_policy_set(allowed_namespaces: tuple[str, ...],
                       max_scale_increase_pct: int = 25) -> PolicySet:
    def action_catalog(inp: dict) -> RuleResult:
        if inp.get("action_type") in ("scale_out", "rollback_release"):
            return "allow", None
        return "deny", f"动作 {inp.get('action_type')} 不在标准目录"

    def namespace_allowlist(inp: dict) -> RuleResult:
        ns = (inp.get("target") or {}).get("namespace")
        if ns in allowed_namespaces:
            return None, None
        return "deny", f"命名空间 {ns} 不在白名单 {list(allowed_namespaces)}"

    def blast_radius(inp: dict) -> RuleResult:
        if inp.get("action_type") != "scale_out":
            return None, None
        params = inp.get("parameters") or {}
        original = params.get("original_replicas")
        target = params.get("target_replicas")
        if not isinstance(original, int) or not isinstance(target, int):
            return "deny", "scale_out 缺少副本参数"
        limit = math.floor(original * max_scale_increase_pct / 100)
        if target - original > limit:
            return "deny", (f"扩容 +{target - original} 超过爆炸半径上限 "
                            f"{max_scale_increase_pct}%(最多 +{limit})")
        return None, None

    return PolicySet(
        version="policy-mvp-1",
        rules=[PolicyRule("action_catalog", action_catalog),
               PolicyRule("namespace_allowlist", namespace_allowlist),
               PolicyRule("blast_radius", blast_radius)])
