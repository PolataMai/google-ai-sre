"""证据存储（F03 存储层）：按事故落盘、追加式、带完整性哈希。

设计：
- 目录式存储：store_dir/<incident_id>.json，每条记录 = {evidence, sha256}；
  sha256 对证据的规范化 JSON 计算——审计时 verify 可发现落盘后被篡改的记录；
- 追加式：同一事故内 evidence_id 不允许覆盖（DuplicateEvidence）——
  证据不可篡改的第一道防线在写入口，第二道在校验哈希；
- 写透（write-through）：add 即落盘，进程崩溃不丢已写证据；
- ingest 直接吞 connectors.ContextBundle，采集完成即入库。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from aisre.connectors import ContextBundle
from aisre.schemas import Evidence


class DuplicateEvidence(ValueError):
    """同一事故内重复的 evidence_id——证据只可追加，不可覆盖。"""


def _digest(evidence: Evidence) -> str:
    canonical = json.dumps(evidence.to_dict(), sort_keys=True,
                           ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


class EvidenceStore:
    def __init__(self, store_dir: str):
        self._dir = Path(store_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, incident_id: str) -> Path:
        return self._dir / f"{incident_id}.json"

    def _load(self, incident_id: str) -> list[dict]:
        path = self._path(incident_id)
        if not path.exists():
            return []
        return json.loads(path.read_text(encoding="utf-8"))

    def add(self, incident_id: str, evidence: Evidence) -> None:
        records = self._load(incident_id)
        if any(r["evidence"]["evidence_id"] == evidence.evidence_id
               for r in records):
            raise DuplicateEvidence(
                f"事故 {incident_id} 已存在证据 {evidence.evidence_id}，"
                f"证据只可追加不可覆盖")
        records.append({"evidence": evidence.to_dict(),
                        "sha256": _digest(evidence)})
        self._path(incident_id).write_text(
            json.dumps(records, ensure_ascii=False, indent=2),
            encoding="utf-8")

    def list(self, incident_id: str) -> list[Evidence]:
        return [Evidence.from_dict(r["evidence"])
                for r in self._load(incident_id)]

    def verify(self, incident_id: str) -> list[str]:
        """重算哈希比对，返回被篡改的 evidence_id 列表；空 = 完整。"""
        corrupted = []
        for r in self._load(incident_id):
            evidence = Evidence.from_dict(r["evidence"])
            if _digest(evidence) != r["sha256"]:
                corrupted.append(evidence.evidence_id)
        return corrupted

    def ingest(self, incident_id: str, bundle: ContextBundle) -> int:
        """把一次并行采集的全部证据入库，返回入库条数。"""
        for evidence in bundle.evidences:
            self.add(incident_id, evidence)
        return len(bundle.evidences)
