"""Gold 数据流程:值班人关单时零负担回流高质量标注。

对应文章的 Golden Data Generation Workflow:事故宣布缓解时,系统从
实际执行的动作主动预填建议,值班人在标准流程里接受/修改/拒绝——
高质量 Gold 标注持续回流,不额外增加值班负担。

- 接受/修改都产出 GoldLabel(source 区分),拒绝不产出;
- GoldStore 追加式 JSONL 落盘(标注历史全保留),按事故取最新;
- Gold 是评测的对照答案:cause_code 用于 Top-3 召回率,
  action 三元组(类型+目标+标准化参数)用于 L2 精确匹配率。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from aisre.actions import ActionPlan
from aisre.scenarios import CauseCode


@dataclass
class GoldLabel:
    incident_id: str
    cause_code: str
    action: dict            # {action_type, target, parameters};无动作事故为 {}
    source: str             # accepted / modified
    labeled_by: str
    labeled_at: str

    def to_dict(self) -> dict:
        return {"incident_id": self.incident_id, "cause_code": self.cause_code,
                "action": self.action, "source": self.source,
                "labeled_by": self.labeled_by, "labeled_at": self.labeled_at}

    @staticmethod
    def from_dict(d: dict) -> "GoldLabel":
        return GoldLabel(**d)


def suggest_from_execution(incident_id: str, executed_plan: ActionPlan,
                           top_cause: str) -> dict:
    """关单时的预填建议:直接取实际执行的动作,值班人只需确认或纠正。"""
    return {
        "incident_id": incident_id,
        "cause_code": top_cause,
        "action": {
            "action_type": executed_plan.action_type,
            "target": dict(executed_plan.target),
            "parameters": dict(executed_plan.parameters),
        },
        "status": "suggested",
    }


def _validate_cause(cause_code: str) -> None:
    try:
        CauseCode(cause_code)
    except ValueError:
        raise ValueError(f"未注册的场景 cause_code: {cause_code}") from None


def accept(suggestion: dict, by: str, at: str) -> GoldLabel:
    _validate_cause(suggestion["cause_code"])
    return GoldLabel(incident_id=suggestion["incident_id"],
                     cause_code=suggestion["cause_code"],
                     action=suggestion["action"], source="accepted",
                     labeled_by=by, labeled_at=at)


def modify(suggestion: dict, by: str, at: str,
           cause_code: Optional[str] = None,
           action: Optional[dict] = None) -> GoldLabel:
    final_cause = cause_code or suggestion["cause_code"]
    _validate_cause(final_cause)
    return GoldLabel(incident_id=suggestion["incident_id"],
                     cause_code=final_cause,
                     action=action or suggestion["action"], source="modified",
                     labeled_by=by, labeled_at=at)


def reject(suggestion: dict) -> None:
    """拒绝:该事故本轮不产出 Gold(等待人工另行标注)。"""
    return None


class GoldStore:
    """追加式 JSONL:标注历史全保留,按事故取最新一条。"""

    def __init__(self, store_dir: str):
        self._path = Path(store_dir) / "gold_labels.jsonl"
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> list[GoldLabel]:
        if not self._path.exists():
            return []
        return [GoldLabel.from_dict(json.loads(line))
                for line in self._path.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    def add(self, label: GoldLabel) -> None:
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(label.to_dict(), ensure_ascii=False) + "\n")

    def count(self) -> int:
        return len(self._load())

    def list(self) -> list[GoldLabel]:
        return self._load()

    def for_incident(self, incident_id: str) -> Optional[GoldLabel]:
        matches = [l for l in self._load() if l.incident_id == incident_id]
        return matches[-1] if matches else None
