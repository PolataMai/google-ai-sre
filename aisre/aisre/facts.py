"""事实抽取:从证据快照确定性地提取带证据的事实。

MVP 阶段用规则替代 LLM 做"聚合去重验证":阈值是模块常量,同样的证据
永远得到同样的事实(事实 id 也确定)——可回放、可评测、可审计。
每条事实天生绑定产生它的证据 id,从源头保证证据覆盖率。

抽取规则(kind → 触发条件):
- error_rate_rise        错误率翻倍且绝对值 ≥ 1%
- latency_rise           p99 延迟 ≥ 1.5 倍
- capacity_saturation    cpu/memory/conn_pool 任一利用率 ≥ 85%
- single_instance_outlier 单实例错误率 ≥ 5 倍于其余实例中位数,且其余正常
- recent_deploy          发布时间落在证据查询窗口内
- log_error_spike        错误日志行数 ≥ 100
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from aisre.schemas import Evidence, Fact

ERROR_RISE_FACTOR = 2.0
ERROR_RISE_FLOOR = 0.01
LATENCY_RISE_FACTOR = 1.5
CAPACITY_SATURATION_PCT = 85.0
OUTLIER_FACTOR = 5.0
OUTLIER_PEER_CEILING = 0.05
LOG_SPIKE_LINES = 100


def _parse_ts(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass
class ExtractedFact:
    kind: str
    fact: Fact
    meta: dict = field(default_factory=dict)   # 供假设引擎使用的结构化细节


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def extract_facts(evidences: list[Evidence]) -> list[ExtractedFact]:
    out: list[ExtractedFact] = []
    seq: dict[str, int] = {}

    def emit(kind: str, text: str, evidence: Evidence, observed_at: str,
             meta: dict | None = None):
        seq[kind] = seq.get(kind, 0) + 1
        out.append(ExtractedFact(
            kind=kind,
            fact=Fact(fact_id=f"fact-{kind}-{seq[kind]}", text=text,
                      observed_at=observed_at,
                      evidence_ids=[evidence.evidence_id]),
            meta=meta or {}))

    for ev in evidences:
        snap = ev.snapshot if isinstance(ev.snapshot, dict) else {}
        window_end = ev.time_range[1]

        if ev.source == "metrics":
            before = snap.get("error_rate_before")
            after = snap.get("error_rate_after")
            if (isinstance(before, (int, float)) and isinstance(after, (int, float))
                    and after >= before * ERROR_RISE_FACTOR
                    and after >= ERROR_RISE_FLOOR):
                emit("error_rate_rise",
                     f"错误率从 {before:.1%} 升至 {after:.1%}",
                     ev, window_end, {"before": before, "after": after})

            lat_before = snap.get("latency_p99_before_ms")
            lat_after = snap.get("latency_p99_after_ms")
            if (isinstance(lat_before, (int, float))
                    and isinstance(lat_after, (int, float))
                    and lat_after >= lat_before * LATENCY_RISE_FACTOR):
                emit("latency_rise",
                     f"p99 延迟从 {lat_before:.0f}ms 升至 {lat_after:.0f}ms",
                     ev, window_end)

            for key, label in (("cpu_utilization_pct", "cpu"),
                               ("memory_utilization_pct", "memory"),
                               ("conn_pool_used_pct", "conn_pool")):
                val = snap.get(key)
                if isinstance(val, (int, float)) and val >= CAPACITY_SATURATION_PCT:
                    emit("capacity_saturation",
                         f"{label} 利用率达 {val:.0f}%（阈值 {CAPACITY_SATURATION_PCT:.0f}%）",
                         ev, window_end, {"resource": label, "pct": val})

            rates = snap.get("instance_error_rates")
            if isinstance(rates, dict) and len(rates) >= 3:
                worst_instance = max(rates, key=rates.get)
                worst = rates[worst_instance]
                peers = [v for k, v in rates.items() if k != worst_instance]
                peer_median = _median(peers)
                if (worst >= max(peer_median, 1e-9) * OUTLIER_FACTOR
                        and peer_median <= OUTLIER_PEER_CEILING):
                    emit("single_instance_outlier",
                         f"实例 {worst_instance} 错误率 {worst:.1%}，"
                         f"其余实例中位数 {peer_median:.1%}",
                         ev, window_end,
                         {"instance": worst_instance, "rate": worst,
                          "peer_median": peer_median})

        elif ev.source == "release":
            deployed_at = snap.get("deployed_at")
            version = snap.get("version")
            if deployed_at and version:
                start, end = (_parse_ts(ev.time_range[0]),
                              _parse_ts(ev.time_range[1]))
                if start <= _parse_ts(deployed_at) <= end:
                    emit("recent_deploy",
                         f"版本 {version} 于 {deployed_at} 发布"
                         f"（上一版本 {snap.get('previous', '未知')}）",
                         ev, deployed_at,
                         {"version": version,
                          "previous": snap.get("previous"),
                          "deployed_at": deployed_at})

        elif ev.source == "logs":
            lines = snap.get("error_lines")
            if isinstance(lines, (int, float)) and lines >= LOG_SPIKE_LINES:
                emit("log_error_spike",
                     f"窗口内错误日志 {lines:.0f} 行（阈值 {LOG_SPIKE_LINES}）",
                     ev, window_end, {"lines": lines})

    return out
