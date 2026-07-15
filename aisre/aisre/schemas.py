"""事件数据契约：证据、事实、假设、告警丰富结果（F03）。

结构性约束（对应"每条事实可追溯"）：
- Fact 必须携带至少一个可解析的 evidence_id —— add_fact 强制；
- 无证据的推测只能作为 Hypothesis（待验证）存在；
- Hypothesis.cause_code 必须是已注册场景（scenarios 封闭枚举）；
- 外部载入（from_dict）不做入口校验，由 validate_enrichment / evidence_coverage
  暴露缺口——覆盖率指标必须能对任意来源的数据计算，不能只对 API 构造的数据成立。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from aisre.scenarios import CauseCode


class MissingEvidence(ValueError):
    """事实没有携带任何 evidence_id。"""


class UnknownEvidence(ValueError):
    """事实引用的 evidence_id 在证据库中不存在。"""


@dataclass
class Evidence:
    evidence_id: str
    source: str                      # metrics / logs / trace / release / config / topology / history
    query: str                       # 原始查询语句或参数
    time_range: tuple[str, str]      # (start, end) ISO 时间
    url: str                         # 可回跳的证据链接
    snapshot: Any = None             # 查询时刻的数据快照（JSON 可序列化）

    def to_dict(self) -> dict:
        return {
            "evidence_id": self.evidence_id,
            "source": self.source,
            "query": self.query,
            "time_range": list(self.time_range),
            "url": self.url,
            "snapshot": self.snapshot,
        }

    @staticmethod
    def from_dict(d: dict) -> "Evidence":
        return Evidence(
            evidence_id=d["evidence_id"],
            source=d["source"],
            query=d["query"],
            time_range=tuple(d["time_range"]),
            url=d["url"],
            snapshot=d.get("snapshot"),
        )


@dataclass
class Fact:
    fact_id: str
    text: str
    observed_at: str
    evidence_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "fact_id": self.fact_id,
            "text": self.text,
            "observed_at": self.observed_at,
            "evidence_ids": list(self.evidence_ids),
        }

    @staticmethod
    def from_dict(d: dict) -> "Fact":
        return Fact(
            fact_id=d["fact_id"],
            text=d["text"],
            observed_at=d["observed_at"],
            evidence_ids=list(d.get("evidence_ids", [])),
        )


@dataclass
class Hypothesis:
    rank: int
    cause_code: str                  # 必须是 scenarios.CauseCode 之一
    evidence_for: list[str]          # 引用 fact_id（可为空 = 待验证假设）
    evidence_against: list[str]
    verification_steps: list[str]
    confidence: float

    def to_dict(self) -> dict:
        return {
            "rank": self.rank,
            "cause_code": self.cause_code,
            "evidence_for": list(self.evidence_for),
            "evidence_against": list(self.evidence_against),
            "verification_steps": list(self.verification_steps),
            "confidence": self.confidence,
        }

    @staticmethod
    def from_dict(d: dict) -> "Hypothesis":
        return Hypothesis(
            rank=d["rank"],
            cause_code=d["cause_code"],
            evidence_for=list(d.get("evidence_for", [])),
            evidence_against=list(d.get("evidence_against", [])),
            verification_steps=list(d.get("verification_steps", [])),
            confidence=d["confidence"],
        )


@dataclass
class Enrichment:
    """一次告警丰富的完整产出：证据库 + 事实列表 + Top-N 假设。"""
    incident_id: str
    alert_received_at: str
    enrichment_published_at: Optional[str] = None
    evidences: dict[str, Evidence] = field(default_factory=dict)
    facts: list[Fact] = field(default_factory=list)
    hypotheses: list[Hypothesis] = field(default_factory=list)

    # ---- 受控入口：从结构上保障证据覆盖 ----

    def add_evidence(self, ev: Evidence) -> None:
        if ev.evidence_id in self.evidences:
            raise ValueError(f"重复的 evidence_id: {ev.evidence_id}")
        self.evidences[ev.evidence_id] = ev

    def add_fact(self, fact: Fact) -> None:
        if not fact.evidence_ids:
            raise MissingEvidence(
                f"事实 {fact.fact_id} 没有证据，只能作为待验证假设进入 hypotheses")
        unknown = [e for e in fact.evidence_ids if e not in self.evidences]
        if unknown:
            raise UnknownEvidence(f"事实 {fact.fact_id} 引用了不存在的证据: {unknown}")
        if any(f.fact_id == fact.fact_id for f in self.facts):
            raise ValueError(f"重复的 fact_id: {fact.fact_id}")
        self.facts.append(fact)

    def add_hypothesis(self, hyp: Hypothesis) -> None:
        try:
            CauseCode(hyp.cause_code)
        except ValueError:
            raise ValueError(f"未注册的场景 cause_code: {hyp.cause_code}") from None
        if not 0.0 <= hyp.confidence <= 1.0:
            raise ValueError(f"confidence 必须在 [0,1]: {hyp.confidence}")
        known_facts = {f.fact_id for f in self.facts}
        unknown = [f for f in hyp.evidence_for + hyp.evidence_against
                   if f not in known_facts]
        if unknown:
            raise ValueError(f"假设引用了不存在的事实: {unknown}")
        self.hypotheses.append(hyp)

    # ---- 序列化（外部载入不校验，由 validate_enrichment 兜底） ----

    def to_dict(self) -> dict:
        return {
            "incident_id": self.incident_id,
            "alert_received_at": self.alert_received_at,
            "enrichment_published_at": self.enrichment_published_at,
            "evidences": [e.to_dict() for e in self.evidences.values()],
            "facts": [f.to_dict() for f in self.facts],
            "hypotheses": [h.to_dict() for h in self.hypotheses],
        }

    @staticmethod
    def from_dict(d: dict) -> "Enrichment":
        enr = Enrichment(
            incident_id=d["incident_id"],
            alert_received_at=d["alert_received_at"],
            enrichment_published_at=d.get("enrichment_published_at"),
        )
        for ed in d.get("evidences", []):
            enr.evidences[ed["evidence_id"]] = Evidence.from_dict(ed)
        enr.facts = [Fact.from_dict(fd) for fd in d.get("facts", [])]
        enr.hypotheses = [Hypothesis.from_dict(hd) for hd in d.get("hypotheses", [])]
        return enr


def evidence_coverage(enr: Enrichment) -> float:
    """证据覆盖率 = 有至少一个可解析证据的事实数 ÷ 全部事实数；无事实记 0。"""
    if not enr.facts:
        return 0.0
    covered = sum(
        1 for f in enr.facts
        if any(e in enr.evidences for e in f.evidence_ids)
    )
    return covered / len(enr.facts)


def validate_enrichment(enr: Enrichment) -> list[str]:
    """对任意来源（含外部载入）的丰富结果做守门校验，返回违规清单。"""
    violations: list[str] = []
    known_facts = {f.fact_id for f in enr.facts}
    for f in enr.facts:
        if not f.evidence_ids:
            violations.append(f"事实 {f.fact_id} 没有证据")
        else:
            unknown = [e for e in f.evidence_ids if e not in enr.evidences]
            if unknown:
                violations.append(f"事实 {f.fact_id} 引用了不存在的证据: {unknown}")
    for h in enr.hypotheses:
        try:
            CauseCode(h.cause_code)
        except ValueError:
            violations.append(f"假设 rank={h.rank} 使用未注册场景 {h.cause_code}")
        if not 0.0 <= h.confidence <= 1.0:
            violations.append(f"假设 rank={h.rank} confidence 越界: {h.confidence}")
        unknown = [x for x in h.evidence_for + h.evidence_against
                   if x not in known_facts]
        if unknown:
            violations.append(f"假设 rank={h.rank} 引用了不存在的事实: {unknown}")
    return violations
