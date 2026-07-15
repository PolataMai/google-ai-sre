"""告警接入（F01）：Webhook 归一 → 统一 incident_id → 幂等去重 → 启动工作流。

设计：
- incident_id = "inc-" + sha1(source|fingerprint)[:12]，确定性——同一告警
  指纹在任何实例、任何时刻都得到同一 id，重复投递天然幂等，回放可复现；
- 去重以 (source, fingerprint) 为键：活跃事故期间的重复告警合并进原事故，
  不重复触发工作流；
- 解析失败显式抛错（UnknownFormat / MalformedPayload），不静默丢弃——
  接入层丢告警是最不可接受的失败模式。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Callable, Optional


class UnknownFormat(ValueError):
    """不认识的 Webhook 格式。"""


class MalformedPayload(ValueError):
    """负载缺少必要字段。"""


@dataclass
class Alert:
    source: str          # alertmanager / pagerduty / custom
    fingerprint: str     # 去重键（来源系统内唯一）
    service: str
    severity: str
    title: str
    starts_at: str
    raw: dict = field(default_factory=dict)


def _require(d: dict, key: str, ctx: str):
    if key not in d or d[key] in (None, "", {}):
        raise MalformedPayload(f"{ctx} 缺少字段 {key}")
    return d[key]


def _parse_alertmanager(payload: dict) -> list[Alert]:
    alerts = _require(payload, "alerts", "alertmanager payload")
    out = []
    for item in alerts:
        labels = _require(item, "labels", "alertmanager alert")
        out.append(Alert(
            source="alertmanager",
            fingerprint=_require(item, "fingerprint", "alertmanager alert"),
            service=_require(labels, "service", "alertmanager labels"),
            severity=labels.get("severity", "unknown"),
            title=labels.get("alertname", ""),
            starts_at=_require(item, "startsAt", "alertmanager alert"),
            raw=item,
        ))
    return out


def _parse_pagerduty(payload: dict) -> list[Alert]:
    event = _require(payload, "event", "pagerduty payload")
    data = _require(event, "data", "pagerduty event")
    return [Alert(
        source="pagerduty",
        fingerprint=_require(data, "id", "pagerduty data"),
        service=_require(_require(data, "service", "pagerduty data"),
                         "summary", "pagerduty service"),
        severity=data.get("urgency", "unknown"),
        title=data.get("title", ""),
        starts_at=_require(data, "created_at", "pagerduty data"),
        raw=data,
    )]


def _parse_custom(payload: dict) -> list[Alert]:
    return [Alert(
        source="custom",
        fingerprint=_require(payload, "alert_id", "custom payload"),
        service=_require(payload, "service", "custom payload"),
        severity=payload.get("severity", "unknown"),
        title=payload.get("title", ""),
        starts_at=_require(payload, "occurred_at", "custom payload"),
        raw=payload,
    )]


_PARSERS = {
    "alertmanager": _parse_alertmanager,
    "pagerduty": _parse_pagerduty,
    "custom": _parse_custom,
}


def parse_webhook(payload: dict, fmt: str) -> list[Alert]:
    try:
        parser = _PARSERS[fmt]
    except KeyError:
        raise UnknownFormat(
            f"未知格式 {fmt}，支持: {sorted(_PARSERS)}") from None
    return parser(payload)


def incident_id_for(alert: Alert) -> str:
    digest = hashlib.sha1(
        f"{alert.source}|{alert.fingerprint}".encode()).hexdigest()
    return f"inc-{digest[:12]}"


@dataclass
class IntakeResult:
    incident_id: str
    created: bool        # False = 重复投递，合并进已有事故
    alert: Alert


class IntakeService:
    """接收 Webhook，去重建事故，新事故触发 on_incident（启动丰富工作流）。"""

    def __init__(self, on_incident: Optional[Callable[[IntakeResult], None]] = None):
        self._on_incident = on_incident
        self._active: dict[str, str] = {}   # (source|fingerprint) -> incident_id

    def intake(self, payload: dict, fmt: str) -> list[IntakeResult]:
        results = []
        for alert in parse_webhook(payload, fmt):
            key = f"{alert.source}|{alert.fingerprint}"
            if key in self._active:
                results.append(IntakeResult(
                    incident_id=self._active[key], created=False, alert=alert))
                continue
            iid = incident_id_for(alert)
            self._active[key] = iid
            result = IntakeResult(incident_id=iid, created=True, alert=alert)
            results.append(result)
            if self._on_incident is not None:
                self._on_incident(result)
        return results
