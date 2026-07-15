"""90 天基线（第 1–2 周交付）：试点前的人工处置对照数据。

口径（必须与后续指标看板一致，保证"每项指标能从记录重算"）：
- 窗口：started_at ∈ (as_of - window_days, as_of]；
- MTTM = mitigated_at - started_at，单位分钟；
- 分位数用 nearest-rank：sorted[ceil(q*n)-1]——确定性、可审计重算，
  代价是小样本下偏保守（不插值）；
- open（未缓解）事故不进 MTTM 统计，计入 open_excluded；
- 变更失败率 = 窗口内 failed 变更 ÷ 窗口内全部变更。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional


def _parse_ts(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _nearest_rank(sorted_values: list[float], q: float) -> float:
    idx = max(0, math.ceil(q * len(sorted_values)) - 1)
    return sorted_values[idx]


@dataclass
class IncidentRecord:
    incident_id: str
    service: str
    cause_code: str
    severity: str                    # S1–S4
    started_at: str                  # 告警触发时间
    mitigated_at: Optional[str]      # 核心 SLI 连续稳定 5 分钟的时刻；None = 仍未缓解


@dataclass
class ChangeRecord:
    change_id: str
    service: str
    deployed_at: str
    failed: bool                     # 导致回滚、SLO 恶化或新事故


@dataclass
class MTTMStats:
    count: int
    mttm_median_min: Optional[float]
    mttm_p75_min: Optional[float]

    def to_dict(self) -> dict:
        return {"count": self.count,
                "mttm_median_min": self.mttm_median_min,
                "mttm_p75_min": self.mttm_p75_min}


@dataclass
class ChangeStats:
    total_changes: int
    failed_changes: int

    @property
    def failure_rate(self) -> float:
        return self.failed_changes / self.total_changes

    def to_dict(self) -> dict:
        return {"total_changes": self.total_changes,
                "failed_changes": self.failed_changes,
                "failure_rate": self.failure_rate}


@dataclass
class BaselineReport:
    as_of: str
    window_days: int
    by_service_scenario: dict[tuple[str, str], MTTMStats] = field(default_factory=dict)
    by_service_severity: dict[tuple[str, str], MTTMStats] = field(default_factory=dict)
    change_stats: dict[str, ChangeStats] = field(default_factory=dict)
    open_excluded: int = 0           # 窗口内但仍未缓解、不进 MTTM 的事故数

    def to_dict(self) -> dict:
        return {
            "as_of": self.as_of,
            "window_days": self.window_days,
            "by_service_scenario": {
                f"{svc}+{code}": s.to_dict()
                for (svc, code), s in sorted(self.by_service_scenario.items())},
            "by_service_severity": {
                f"{svc}+{sev}": s.to_dict()
                for (svc, sev), s in sorted(self.by_service_severity.items())},
            "change_stats": {svc: s.to_dict()
                             for svc, s in sorted(self.change_stats.items())},
            "open_excluded": self.open_excluded,
        }


def _mttm_minutes(rec: IncidentRecord) -> float:
    delta = _parse_ts(rec.mitigated_at) - _parse_ts(rec.started_at)
    return delta.total_seconds() / 60.0


def _stats(values: list[float], count: int) -> MTTMStats:
    if not values:
        return MTTMStats(count=count, mttm_median_min=None, mttm_p75_min=None)
    ordered = sorted(values)
    return MTTMStats(count=count,
                     mttm_median_min=_nearest_rank(ordered, 0.5),
                     mttm_p75_min=_nearest_rank(ordered, 0.75))


def compute_baseline(incidents: list[IncidentRecord],
                     changes: list[ChangeRecord],
                     as_of: str, window_days: int = 90) -> BaselineReport:
    end = _parse_ts(as_of)
    start = end - timedelta(days=window_days)

    report = BaselineReport(as_of=as_of, window_days=window_days)

    scenario_values: dict[tuple[str, str], list[float]] = {}
    severity_values: dict[tuple[str, str], list[float]] = {}

    for rec in incidents:
        t = _parse_ts(rec.started_at)
        if not (start < t <= end):
            continue
        if rec.mitigated_at is None:
            report.open_excluded += 1
            continue
        m = _mttm_minutes(rec)
        scenario_values.setdefault((rec.service, rec.cause_code), []).append(m)
        severity_values.setdefault((rec.service, rec.severity), []).append(m)

    report.by_service_scenario = {
        key: _stats(vals, len(vals)) for key, vals in scenario_values.items()}
    report.by_service_severity = {
        key: _stats(vals, len(vals)) for key, vals in severity_values.items()}

    counter: dict[str, list[int]] = {}
    for c in changes:
        t = _parse_ts(c.deployed_at)
        if not (start < t <= end):
            continue
        total_failed = counter.setdefault(c.service, [0, 0])
        total_failed[0] += 1
        if c.failed:
            total_failed[1] += 1
    report.change_stats = {
        svc: ChangeStats(total_changes=t, failed_changes=f)
        for svc, (t, f) in counter.items()}

    return report
