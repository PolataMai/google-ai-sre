"""变更三道门：事前准入(precheck) / 事中观测(watch) / 事后验收(accept)。

外加异常关联(correlate)：验收失败或告警时，把异常时刻与时间线上的
最近变更做可解释排序，并可导出 rca 引擎的 audit.json 做深度根因定位。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Optional

from .deps import ServiceGraph, _norm
from .schemas import (ChangeEvent, RiskLevel, RiskProfile, Source, Status,
                      parse_ts)
from .risk import RiskContext, profile_change
from .timeline import Timeline


# ------------------------------------------------------------ 事前：precheck ----

@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    blocking: bool = False


@dataclass
class PrecheckReport:
    change_id: str
    verdict: str                 # PASS | WARN | BLOCK
    checks: list[Check] = field(default_factory=list)


def precheck(ev: ChangeEvent, profile: RiskProfile,
             ctx: RiskContext) -> PrecheckReport:
    checks: list[Check] = []
    high = profile.level == RiskLevel.HIGH
    core = {_norm(c) for c in ctx.core_services}

    # 1. 影响范围（信息项，不拦截）
    checks.append(Check(
        "影响范围", True,
        f"直接服务 {ev.service}；下游 {len(profile.blast_services)} 个："
        f"{', '.join(profile.blast_services) or '无'}"))

    # 2. 高风险依赖：爆炸半径内的核心服务逐个点名
    risky = sorted(set(profile.blast_services) & core)
    checks.append(Check(
        "高风险依赖", not risky,
        f"下游命中核心链路：{', '.join(risky)}" if risky else "下游无核心链路服务"))

    # 3. 回滚能力：HIGH 必须有，MEDIUM 建议有
    if not ev.rollback_plan:
        checks.append(Check("回滚能力", False,
                            "未声明回滚方案" + ("——HIGH 风险变更缺回滚，拦截" if high else ""),
                            blocking=high))
    else:
        checks.append(Check("回滚能力", True, f"回滚方案：{ev.rollback_plan}"))

    # 4. 灰度能力：HIGH 的代码/配置变更必须灰度（DB/infra 用回滚兜底）
    need_gray = high and ev.source in (Source.CODE, Source.CONFIG)
    if not ev.gray:
        checks.append(Check("灰度能力", False,
                            "未声明灰度/分批" + ("——HIGH 风险代码/配置变更须灰度，拦截" if need_gray else ""),
                            blocking=need_gray))
    else:
        checks.append(Check("灰度能力", True, "已声明灰度/分批发布"))

    # 5. 风险画像摘要（信息项）
    checks.append(Check(
        "风险画像", profile.level != RiskLevel.HIGH,
        f"{profile.level.value}（{profile.score} 分）："
        + "；".join(f"{f.name}+{f.points} {f.evidence}" for f in profile.factors)))

    if any(c.blocking for c in checks):
        verdict = "BLOCK"
    elif any(not c.ok for c in checks):
        verdict = "WARN"
    else:
        verdict = "PASS"
    return PrecheckReport(change_id=ev.change_id, verdict=verdict, checks=checks)


# ------------------------------------------------------------ 事中：watch ----

@dataclass
class MetricDrift:
    metric: str
    baseline_mean: float
    post_mean: float
    delta_pct: float
    zscore: float
    drifted: bool
    detail: str = ""


@dataclass
class WatchReport:
    change_id: str
    verdict: str                 # STEADY | DRIFTED
    drifts: list[MetricDrift] = field(default_factory=list)


def _window_values(series: list[dict], start, end) -> list[float]:
    return [float(p["value"]) for p in series
            if start <= parse_ts(p["ts"]) <= end]


def _mean_std(vals: list[float]) -> tuple[float, float]:
    if not vals:
        return 0.0, 0.0
    m = sum(vals) / len(vals)
    var = sum((v - m) ** 2 for v in vals) / len(vals)
    return m, math.sqrt(var)


def watch(ev: ChangeEvent, metrics: dict[str, list[dict]],
          baseline_min: int = 30, post_min: int = 30,
          rel_threshold: float = 0.25, z_threshold: float = 3.0) -> WatchReport:
    """基线窗 [t-baseline, t) vs 观察窗 (t, t+post]：均值漂移检测。

    双条件：z 分数超阈 **且** 相对变化超阈才算漂移——只用 z 会在方差极小的
    平稳指标上误报（0.1% 的抖动也能 z>3），只用相对值会漏掉低基数指标。
    """
    t = parse_ts(ev.timestamp)
    drifts = []
    for name, series in sorted(metrics.items()):
        base = _window_values(series, t - timedelta(minutes=baseline_min), t)
        post = _window_values(series, t, t + timedelta(minutes=post_min))
        if not base or not post:
            drifts.append(MetricDrift(name, 0, 0, 0, 0, False, "窗口内无数据，跳过"))
            continue
        bm, bs = _mean_std(base)
        pm, _ = _mean_std(post)
        delta_pct = (pm - bm) / bm if bm else (math.inf if pm else 0.0)
        z = (pm - bm) / bs if bs else (math.inf if pm != bm else 0.0)
        drifted = abs(delta_pct) >= rel_threshold and abs(z) >= z_threshold
        drifts.append(MetricDrift(
            name, round(bm, 4), round(pm, 4), round(delta_pct, 4),
            round(z, 2) if math.isfinite(z) else math.inf, drifted,
            f"基线均值 {bm:.4g} → 观察均值 {pm:.4g}（Δ{delta_pct:+.1%}）"))
    verdict = "DRIFTED" if any(d.drifted for d in drifts) else "STEADY"
    return WatchReport(change_id=ev.change_id, verdict=verdict, drifts=drifts)


# ------------------------------------------------------------ 事后：accept ----

@dataclass
class AcceptReport:
    change_id: str
    verdict: str                 # ACCEPTED | REJECTED
    drift: WatchReport = None
    rule_violations: list[str] = field(default_factory=list)


def accept(ev: ChangeEvent, metrics: dict[str, list[dict]],
           rules: Optional[dict] = None, post_min: int = 30,
           baseline_min: int = 30) -> AcceptReport:
    """自动验收 = 漂移检测 + 硬阈值规则（rules: {metric: {"max": x, "min": y}}）。"""
    w = watch(ev, metrics, baseline_min=baseline_min, post_min=post_min)
    violations = []
    t = parse_ts(ev.timestamp)
    for metric, rule in (rules or {}).items():
        post = _window_values(metrics.get(metric, []),
                              t, t + timedelta(minutes=post_min))
        if not post:
            violations.append(f"{metric}: 验收窗口无数据（观测缺失本身即不通过）")
            continue
        worst_max, worst_min = max(post), min(post)
        if "max" in rule and worst_max > rule["max"]:
            violations.append(f"{metric}: 峰值 {worst_max:.4g} 超上限 {rule['max']}")
        if "min" in rule and worst_min < rule["min"]:
            violations.append(f"{metric}: 谷值 {worst_min:.4g} 低于下限 {rule['min']}")
    verdict = "ACCEPTED" if (w.verdict == "STEADY" and not violations) else "REJECTED"
    return AcceptReport(change_id=ev.change_id, verdict=verdict,
                        drift=w, rule_violations=violations)


# ------------------------------------------------------ 异常 → 变更关联 ----

@dataclass
class Suspect:
    change_id: str
    score: int
    reasons: list[str] = field(default_factory=list)
    event: ChangeEvent = None


def correlate(timeline: Timeline, anomaly_ts: str, anomaly_service: str,
              ctx: RiskContext, window_hours: int = 24,
              top: int = 5) -> list[Suspect]:
    """异常时刻 × 时间线 → 嫌疑变更排序（时间邻近 + 服务关联 + 风险画像）。

    已回滚的变更不参与排序（它不在场）；输出可解释 reasons，
    深度定位交给 rca 引擎（export_rca_audit + rca run）。
    """
    svc = _norm(anomaly_service)
    upstream = set(ctx.graph.dependencies_of(svc))
    t = parse_ts(anomaly_ts)
    suspects: list[Suspect] = []
    for ev in timeline.window_before(anomaly_ts, window_hours):
        if ev.status == Status.ROLLED_BACK:
            continue
        reasons = []
        hours_ago = (t - parse_ts(ev.timestamp)).total_seconds() / 3600
        time_pts = 40 if hours_ago <= 1 else 30 if hours_ago <= 6 else 20
        reasons.append(f"异常前 {hours_ago:.1f}h 内的变更")

        ev_svc = _norm(ev.service)
        if ev_svc == svc:
            rel_pts = 30
            reasons.append("同服务变更")
        elif ev_svc in upstream:
            rel_pts = 20
            reasons.append(f"异常服务的上游依赖（{svc} → {ev_svc}）")
        elif svc in ctx.graph.blast_radius(ev_svc):
            rel_pts = 20
            reasons.append(f"其爆炸半径覆盖异常服务")
        else:
            rel_pts = 0

        prof = profile_change(ev, ctx)
        risk_pts = prof.score // 5
        reasons.append(f"风险画像 {prof.level.value}({prof.score})")

        suspects.append(Suspect(change_id=ev.change_id,
                                score=time_pts + rel_pts + risk_pts,
                                reasons=reasons, event=ev))
    suspects.sort(key=lambda s: -s.score)
    return suspects[:top]


# ------------------------------------------------------ rca 引擎衔接 ----

_RCA_TYPE = {Source.CONFIG: "config", Source.DB: "db",
             Source.MIDDLEWARE: "infra", Source.INFRA: "infra"}


def export_rca_audit(events: list[ChangeEvent]) -> list[dict]:
    """非代码变更导出为 rca audit.json 契约（代码变更 rca 直接从 git 取证，不经此路）。"""
    out = []
    for ev in events:
        if ev.source == Source.CODE:
            continue
        entry = {
            "id": ev.change_id, "type": _RCA_TYPE[ev.source],
            "service": ev.service, "timestamp": ev.timestamp,
            "summary": ev.summary, "author": ev.author,
            "keys": ev.scope_items(),
        }
        if ev.details.get("relation"):
            entry["relation"] = ev.details["relation"]
        out.append(entry)
    return out
