"""故障知识库：指纹 → 历史根因，形成"每次 RCA 结束回写"的闭环。

存储为单个 JSON 文件（生产上可换成 graphify 知识图谱后端，契约不变）：
{ "<fingerprint>": [ {incident_id, date, tier, root_cause, change_id, notes}, ... ] }
"""
from __future__ import annotations

import json
from pathlib import Path

from .schemas import KbEntry, RcaReport, Tier


class KnowledgeBase:
    def __init__(self, path: str):
        self.path = Path(path)
        if self.path.exists():
            self._data: dict[str, list[dict]] = json.loads(
                self.path.read_text(encoding="utf-8"))
        else:
            self._data = {}

    def lookup(self, fingerprint: str) -> list[KbEntry]:
        return [KbEntry(fingerprint=fingerprint, **e)
                for e in self._data.get(fingerprint, [])]

    def write_back(self, report: RcaReport) -> int:
        """把 CONFIRMED / LIKELY 结论回写知识库，返回写入条数。

        HYPOTHESIS 不回写——未定案的猜想进知识库会污染后续检索。
        """
        sig_by_fp = {s.fingerprint: s for s in report.signatures}
        cand_by_id = {c.change_id: c for c in report.candidates}
        written = 0
        for v in report.verdicts:
            if v.tier == Tier.HYPOTHESIS:
                continue
            sig = sig_by_fp.get(v.fingerprint)
            cand = cand_by_id.get(v.change_id) if v.change_id else None
            root_cause = v.explanation or (cand.summary if cand else "")
            entry = {
                "incident_id": report.incident_id,
                "date": report.alert_time,
                "tier": v.tier.value,
                "root_cause": f"[{sig.exception_type}] {root_cause}" if sig else root_cause,
                "change_id": v.change_id or "",
                "notes": "; ".join(e.detail for e in v.evidence_chain[:3]),
            }
            bucket = self._data.setdefault(v.fingerprint, [])
            if any(e.get("incident_id") == report.incident_id for e in bucket):
                continue  # 幂等：同一事故不重复写
            bucket.append(entry)
            written += 1
        if written:
            self.save()
        return written

    def add_entry(self, fingerprint: str, entry: dict) -> None:
        """追加/更新一条定案记录（agent 反驳验证通过后回写最终行为差异解释）。

        同一 incident_id 已存在时按定案信息覆盖（--write-back 的初稿被精修稿替换）。
        """
        bucket = self._data.setdefault(fingerprint, [])
        for e in bucket:
            if e.get("incident_id") == entry.get("incident_id"):
                e.update(entry)
                self.save()
                return
        bucket.append(entry)
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
