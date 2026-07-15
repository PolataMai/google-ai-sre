"""Top-3 根因分析(F04):对三类场景确定性打分。

MVP 用规则打分替代 LLM 推理:置信度是常量表,排序稳定,
同样的事实集合永远得到同样的 Top-3——这让"Top-3 召回率 ≥85%"
成为可以对 Gold 数据精确重算的指标,而不是一次性的模型输出。

打分规则:
- RECENT_RELEASE_REGRESSION:窗口内有发布 + 错误/延迟上升,且发布早于
  上升观测点 → 0.90;上升早于发布(时序矛盾)→ 发布事实进反对证据,0.20;
  只有发布无上升 → 0.30;无发布 → 0.10(待验证);
- CAPACITY_SATURATION:有容量饱和事实 → 0.80;无 → 0.10;
- SINGLE_INSTANCE_ANOMALY:有单实例离群事实 → 0.85;无 → 0.10。
并列时按场景枚举顺序稳定排序。验证步骤取自场景定义。
"""
from __future__ import annotations

from datetime import datetime, timezone

from aisre.facts import ExtractedFact
from aisre.scenarios import CauseCode, get_scenario
from aisre.schemas import Hypothesis

CONF_RELEASE_STRONG = 0.90
CONF_RELEASE_CONTRADICTED = 0.20
CONF_RELEASE_DEPLOY_ONLY = 0.30
CONF_CAPACITY = 0.80
CONF_OUTLIER = 0.85
CONF_UNVERIFIED = 0.10


def _parse_ts(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _score_release(extracted: list[ExtractedFact]):
    deploys = [f for f in extracted if f.kind == "recent_deploy"]
    rises = [f for f in extracted
             if f.kind in ("error_rate_rise", "latency_rise")]
    if not deploys:
        return CONF_UNVERIFIED, [], []
    if not rises:
        return CONF_RELEASE_DEPLOY_ONLY, [f.fact.fact_id for f in deploys], []
    deploy = deploys[0]
    deployed_at = _parse_ts(deploy.meta["deployed_at"])
    rise_ts = min(_parse_ts(f.fact.observed_at) for f in rises)
    if deployed_at <= rise_ts:
        evidence_for = [deploy.fact.fact_id] + [f.fact.fact_id for f in rises]
        return CONF_RELEASE_STRONG, evidence_for, []
    # 时序矛盾:错误上升早于发布 → 发布是反证
    return (CONF_RELEASE_CONTRADICTED,
            [f.fact.fact_id for f in rises],
            [deploy.fact.fact_id])


def _score_capacity(extracted: list[ExtractedFact]):
    hits = [f for f in extracted if f.kind == "capacity_saturation"]
    if hits:
        return CONF_CAPACITY, [f.fact.fact_id for f in hits], []
    return CONF_UNVERIFIED, [], []


def _score_outlier(extracted: list[ExtractedFact]):
    hits = [f for f in extracted if f.kind == "single_instance_outlier"]
    if hits:
        return CONF_OUTLIER, [f.fact.fact_id for f in hits], []
    return CONF_UNVERIFIED, [], []


_SCORERS = {
    CauseCode.RECENT_RELEASE_REGRESSION: _score_release,
    CauseCode.CAPACITY_SATURATION: _score_capacity,
    CauseCode.SINGLE_INSTANCE_ANOMALY: _score_outlier,
}


def generate_hypotheses(extracted: list[ExtractedFact]) -> list[Hypothesis]:
    """永远返回全部三个场景的候选,按置信度降序编 rank 1–3。"""
    scored = []
    for order, (code, scorer) in enumerate(_SCORERS.items()):
        confidence, evidence_for, evidence_against = scorer(extracted)
        scored.append((confidence, order, code, evidence_for, evidence_against))
    scored.sort(key=lambda item: (-item[0], item[1]))   # 并列按枚举顺序稳定

    return [
        Hypothesis(
            rank=i + 1,
            cause_code=code.value,
            evidence_for=evidence_for,
            evidence_against=evidence_against,
            verification_steps=list(get_scenario(code).verification_steps),
            confidence=confidence,
        )
        for i, (confidence, _, code, evidence_for, evidence_against)
        in enumerate(scored)
    ]
