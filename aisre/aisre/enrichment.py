"""告警丰富编排(第 5–6 周):采集 → 入库 → 事实 → Top-3 → 校验 → 发布。

90 秒预算的机制化:
- collect 阶段的单源超时即 40s 并行查询预算(collect_context 内部标缺失);
- 缺失源不阻塞发布:partial=True 先发布可用结果,refresh_missing 事后追加;
- 发布前必过 validate_enrichment 守门(违规清单为空才算合规发布);
- p95 口径 = enrichment_published_at - alert_received_at:发布时间由调用方
  注入墙钟时间(写回事故平台成功的时刻),不是模型/管线内部耗时;
- 各阶段耗时(collect/aggregate/reason/validate)记录在 stage_seconds,
  供指标看板定位预算超支的环节。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from aisre.connectors import (ConnectorResult, ReadOnlyConnector,
                              collect_context)
from aisre.evidence_store import EvidenceStore
from aisre.facts import ExtractedFact, extract_facts
from aisre.hypotheses import generate_hypotheses
from aisre.intake import Alert
from aisre.schemas import Enrichment, validate_enrichment

DEFAULT_PER_SOURCE_TIMEOUT = 40.0


def _parse_ts(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass
class EnrichmentRun:
    enrichment: Enrichment
    extracted: list[ExtractedFact]
    results: list[ConnectorResult]
    service: str
    time_range: tuple[str, str]
    partial: bool
    stage_seconds: dict[str, float] = field(default_factory=dict)
    violations: list[str] = field(default_factory=list)

    @property
    def missing_sources(self) -> list[str]:
        return [r.source for r in self.results if r.status != "ok"]


def _assemble(incident_id: str, alert_received_at: str,
              evidences, results, service, time_range,
              published_at: str, stage_seconds: dict[str, float],
              clock: Callable[[], float]) -> EnrichmentRun:
    """聚合 → 推理 → 校验 → 发布(run 与 refresh 共用的后半段)。"""
    t = clock()
    extracted = extract_facts(evidences)
    stage_seconds["aggregate"] = clock() - t

    enr = Enrichment(incident_id=incident_id,
                     alert_received_at=alert_received_at)
    for ev in evidences:
        enr.add_evidence(ev)
    for ef in extracted:
        enr.add_fact(ef.fact)

    t = clock()
    for hyp in generate_hypotheses(extracted):
        enr.add_hypothesis(hyp)
    stage_seconds["reason"] = clock() - t

    t = clock()
    violations = validate_enrichment(enr)
    stage_seconds["validate"] = clock() - t

    enr.enrichment_published_at = published_at
    return EnrichmentRun(
        enrichment=enr, extracted=extracted, results=list(results),
        service=service, time_range=time_range,
        partial=any(r.status != "ok" for r in results),
        stage_seconds=stage_seconds, violations=violations)


def run_enrichment(incident_id: str, alert: Alert,
                   time_range: tuple[str, str],
                   connectors: list[ReadOnlyConnector],
                   store: EvidenceStore,
                   published_at: str,
                   per_source_timeout: float = DEFAULT_PER_SOURCE_TIMEOUT,
                   clock: Callable[[], float] = time.monotonic) -> EnrichmentRun:
    stage_seconds: dict[str, float] = {}
    t = clock()
    bundle = collect_context(alert.service, time_range, connectors,
                             per_source_timeout)
    stage_seconds["collect"] = clock() - t
    store.ingest(incident_id, bundle)

    return _assemble(incident_id, alert.starts_at, bundle.evidences,
                     bundle.results, alert.service, time_range,
                     published_at, stage_seconds, clock)


def refresh_missing(run: EnrichmentRun,
                    connectors: list[ReadOnlyConnector],
                    store: EvidenceStore,
                    published_at: str,
                    per_source_timeout: float = DEFAULT_PER_SOURCE_TIMEOUT,
                    clock: Callable[[], float] = time.monotonic) -> EnrichmentRun:
    """事后追加:只重查缺失源,补证据后重算事实与假设,incident 不变。"""
    missing = set(run.missing_sources)
    retry_connectors = [c for c in connectors if c.source in missing]
    stage_seconds: dict[str, float] = {}
    t = clock()
    bundle = collect_context(run.service, run.time_range, retry_connectors,
                             per_source_timeout)
    stage_seconds["collect"] = clock() - t
    store.ingest(run.enrichment.incident_id, bundle)

    merged_results = ([r for r in run.results if r.source not in missing]
                      + bundle.results)
    evidences = store.list(run.enrichment.incident_id)
    return _assemble(run.enrichment.incident_id,
                     run.enrichment.alert_received_at, evidences,
                     merged_results, run.service, run.time_range,
                     published_at, stage_seconds, clock)


def enrichment_latency_seconds(enr: Enrichment) -> Optional[float]:
    """p95 口径:发布成功时刻 - 收到告警时刻(墙钟秒)。未发布返回 None。"""
    if not enr.enrichment_published_at:
        return None
    delta = (_parse_ts(enr.enrichment_published_at)
             - _parse_ts(enr.alert_received_at))
    return delta.total_seconds()


def p95_seconds(durations: list[float]) -> Optional[float]:
    """nearest-rank p95,与基线口径一致;空列表返回 None。"""
    if not durations:
        return None
    ordered = sorted(durations)
    idx = max(0, -(-len(ordered) * 95 // 100) - 1)   # ceil(0.95n)-1
    return ordered[idx]
