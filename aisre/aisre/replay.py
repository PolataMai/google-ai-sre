"""时间切片回放与 Shadow 日志(F11)。

回放 = 用录制的时间切片快照构造连接器,跑与线上完全相同的
run_enrichment + draft_plan 代码路径——不是模拟,是同一套逻辑换数据源。
快照里缺的源回放为"当时不可用"(missing),保持与线上一致的降级行为。

ShadowLog 追加式落盘每次"只生成不执行"的计划(或拒绝原因),
案例数直接服务 L3 准入的 500 例门槛。
"""
from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from aisre.actions import ActionPlan
from aisre.connectors import ReadOnlyConnector, default_connectors
from aisre.enrichment import EnrichmentRun, run_enrichment
from aisre.evidence_store import EvidenceStore
from aisre.intake import Alert
from aisre.planner import draft_plan

SOURCES = ("metrics", "logs", "trace", "release", "topology")


@dataclass
class ReplayCase:
    case_id: str
    alert: Alert
    time_range: tuple[str, str]
    target: dict
    snapshots: dict                     # source -> 录制时的快照;缺 = 当时不可用
    gold: Optional[dict] = None         # {cause_code, action} 或 None

    def to_dict(self) -> dict:
        d = {
            "case_id": self.case_id,
            "alert": {"source": self.alert.source,
                      "fingerprint": self.alert.fingerprint,
                      "service": self.alert.service,
                      "severity": self.alert.severity,
                      "title": self.alert.title,
                      "starts_at": self.alert.starts_at},
            "time_range": list(self.time_range),
            "target": dict(self.target),
            "snapshots": self.snapshots,
        }
        if self.gold is not None:
            d["gold"] = self.gold
        return d

    @staticmethod
    def from_dict(d: dict) -> "ReplayCase":
        return ReplayCase(
            case_id=d["case_id"],
            alert=Alert(**d["alert"]),
            time_range=tuple(d["time_range"]),
            target=dict(d["target"]),
            snapshots=dict(d["snapshots"]),
            gold=d.get("gold"))


def _replay_connectors(case: ReplayCase) -> list[ReadOnlyConnector]:
    def recorded(source: str):
        snapshot = case.snapshots.get(source)
        if snapshot is None:
            def unavailable(service, time_range):
                raise ConnectionError(f"{source} 在事故时刻不可用（未录制）")
            return unavailable

        def fetch(service, time_range):
            return {"url": f"replay://{case.case_id}/{source}",
                    "query": f"time_slice({time_range[0]},{time_range[1]})",
                    "snapshot": snapshot}
        return fetch

    return default_connectors(**{s: recorded(s) for s in SOURCES})


@dataclass
class ReplayResult:
    case_id: str
    run: EnrichmentRun
    plan: Optional[ActionPlan]
    plan_refusal: Optional[str]
    gold: Optional[dict] = None

    @property
    def top3(self) -> list[str]:
        return [h.cause_code for h in self.run.enrichment.hypotheses]


def replay_case(case: ReplayCase) -> ReplayResult:
    with tempfile.TemporaryDirectory(prefix="aisre-replay-") as tmp:
        run = run_enrichment(
            incident_id=f"replay-{case.case_id}",
            alert=case.alert,
            time_range=case.time_range,
            connectors=_replay_connectors(case),
            store=EvidenceStore(tmp),
            published_at=case.time_range[1])
    plan, refusal = draft_plan(run, target=case.target,
                               expires_at=case.time_range[1])
    return ReplayResult(case_id=case.case_id, run=run, plan=plan,
                        plan_refusal=refusal, gold=case.gold)


class ShadowLog:
    """Shadow 记录:只生成不执行的计划(或拒绝原因),追加式落盘。"""

    def __init__(self, store_dir: str):
        self._path = Path(store_dir) / "shadow_log.jsonl"
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, result: ReplayResult, at: str) -> None:
        entry = {
            "case_id": result.case_id,
            "incident_id": result.run.enrichment.incident_id,
            "top3": result.top3,
            "plan": result.plan.to_dict() if result.plan else None,
            "plan_refusal": result.plan_refusal,
            "recorded_at": at,
        }
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def list(self) -> list[dict]:
        if not self._path.exists():
            return []
        return [json.loads(line)
                for line in self._path.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    def count(self) -> int:
        return len(self.list())
