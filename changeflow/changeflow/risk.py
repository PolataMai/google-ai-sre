"""变更风险画像：五因子规则式评分，可解释、可审计。

因子：范围(scope) / 爆炸半径(blast_radius) / 核心链路(core_link) /
历史故障(history) / 测试覆盖率(coverage) / 发布时间(timing) / 能力声明(capability)。

设计取舍：v1 用透明规则而非模型——变更准入是要跟人吵架的场景，
每一分都必须有 evidence 支撑；分数只是排序器，BLOCK/PASS 由 gates 决定。
"""
from __future__ import annotations

import json
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

from .deps import ServiceGraph, _norm
from .schemas import (ChangeEvent, RiskFactor, RiskLevel, RiskProfile, Source,
                      parse_ts)


@dataclass
class RiskContext:
    graph: ServiceGraph
    core_services: set = field(default_factory=set)      # 核心链路服务（订单/支付/…）
    incident_counts: dict = field(default_factory=dict)  # service → 历史故障次数
    coverage: dict = field(default_factory=dict)         # service → 行覆盖率 0-100

    @staticmethod
    def load_incidents(path: str) -> dict:
        """事故记录 adapter：{service: count} 或 [{service, incident_id, ...}]。"""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {_norm(k): int(v) for k, v in data.items()}
        counts: dict[str, int] = {}
        for e in data:
            s = _norm(e.get("service", ""))
            counts[s] = counts.get(s, 0) + 1
        return counts

    @staticmethod
    def load_coverage(path: str) -> dict:
        return {_norm(k): float(v) for k, v in
                json.loads(Path(path).read_text(encoding="utf-8")).items()}


def _scope_factor(ev: ChangeEvent) -> RiskFactor:
    items = ev.scope_items()
    n = len(items)
    sample = ", ".join(items[:5]) + ("…" if n > 5 else "")
    if ev.is_ddl():
        return RiskFactor("scope", 20, f"DDL 变更 {n} 张表：{sample or '(未声明表)'}")
    if ev.source == Source.CODE:
        pts = 20 if n >= 20 else 10 if n >= 5 else 5
        return RiskFactor("scope", pts, f"改动 {n} 个文件：{sample}")
    if ev.source == Source.CONFIG:
        pts = 10 if n >= 5 else 5
        return RiskFactor("scope", pts, f"改动 {n} 个配置键：{sample}")
    return RiskFactor("scope", 10, f"{ev.source.value} 变更：{sample or ev.summary}")


def profile_change(ev: ChangeEvent, ctx: RiskContext) -> RiskProfile:
    factors: list[RiskFactor] = [_scope_factor(ev)]
    svc = _norm(ev.service)

    blast = ctx.graph.blast_radius(svc)
    if blast:
        factors.append(RiskFactor(
            "blast_radius", min(20, 4 * len(blast)),
            f"{len(blast)} 个下游依赖它：{', '.join(blast[:8])}"
            f"{'…' if len(blast) > 8 else ''}"))

    core_hit = sorted(({svc} | set(blast)) & {_norm(c) for c in ctx.core_services})
    if core_hit:
        factors.append(RiskFactor(
            "core_link", 20, f"命中核心链路服务：{', '.join(core_hit)}"))

    inc = ctx.incident_counts.get(svc, 0)
    if inc:
        factors.append(RiskFactor(
            "history", min(15, 5 * inc), f"{ev.service} 近期有 {inc} 次故障记录"))

    cov = ctx.coverage.get(svc)
    if ev.source == Source.CODE:
        if cov is None:
            factors.append(RiskFactor("coverage", 10, "无覆盖率数据（按未知从严）"))
        elif cov < 50:
            factors.append(RiskFactor("coverage", 15, f"行覆盖率 {cov:.0f}% < 50%"))
        elif cov < 70:
            factors.append(RiskFactor("coverage", 8, f"行覆盖率 {cov:.0f}% < 70%"))

    dt = parse_ts(ev.timestamp)
    if dt.weekday() == 4 and dt.hour >= 16:
        factors.append(RiskFactor("timing", 10, f"周五 {dt.hour} 点后变更（周末前窗口）"))
    elif dt.weekday() >= 5:
        factors.append(RiskFactor("timing", 8, "周末变更"))
    elif 0 <= dt.hour < 6 and not ev.gray:
        factors.append(RiskFactor("timing", 5, "凌晨全量变更（低峰但无人盯盘）"))

    if not ev.gray and ev.source in (Source.CODE, Source.CONFIG):
        factors.append(RiskFactor("capability", 5, "未声明灰度/分批"))
    if not ev.rollback_plan:
        factors.append(RiskFactor("capability", 10, "未声明回滚方案"))

    score = min(100, sum(f.points for f in factors))
    level = (RiskLevel.HIGH if score >= 60
             else RiskLevel.MEDIUM if score >= 35 else RiskLevel.LOW)
    return RiskProfile(change_id=ev.change_id, score=score, level=level,
                       factors=factors, blast_services=blast)
