"""生产 Shadow(F11):线上对真实告警只生成计划、绝不执行。

Shadow 与 L2/L3 的区别不在能不能生成计划,而在生成后是否提交执行:
shadow_evaluate 只调 planner.draft_plan,产出 mode="shadow" 的记录——
本模块不 import gateway、不接触任何执行器,"不执行"是结构性的、不靠自觉
(test_shadow 用源码扫描断言这一点)。

ShadowLedger 追加式累积记录,计数直接服务 L3 准入的 500 例门槛;
与执行路径(gateway)共用同一个 EnrichmentRun + draft_plan,保证 Shadow
观察到的计划与将来真执行的计划一致,累积的案例才有评测价值。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from aisre.enrichment import EnrichmentRun
from aisre.planner import draft_plan


@dataclass
class ShadowRecord:
    incident_id: str
    top3: list[str]
    plan: Optional[dict]               # 生成的 ActionPlan(dict);无则为 None
    plan_refusal: Optional[str]        # 未生成计划的机器可读原因
    mode: str = "shadow"               # 恒为 shadow——记录不执行

    def to_dict(self) -> dict:
        return asdict(self)


def shadow_evaluate(run: EnrichmentRun, target: dict,
                    expires_at: str) -> ShadowRecord:
    """对一次真实告警的丰富结果生成计划草案并记录,绝不提交执行。"""
    plan, refusal = draft_plan(run, target=target, expires_at=expires_at)
    return ShadowRecord(
        incident_id=run.enrichment.incident_id,
        top3=[h.cause_code for h in run.enrichment.hypotheses],
        plan=plan.to_dict() if plan else None,
        plan_refusal=refusal)


class ShadowLedger:
    """Shadow 记录追加式 JSONL:累积案例数供 L3 准入门槛计算。"""

    def __init__(self, store_dir: str):
        self._path = Path(store_dir) / "shadow_ledger.jsonl"
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: ShadowRecord) -> None:
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")

    def list(self) -> list[dict]:
        if not self._path.exists():
            return []
        return [json.loads(line)
                for line in self._path.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    def count(self) -> int:
        return len(self.list())
