"""变更时间线：append-only JSONL 存储，一行一个 ChangeEvent。

- 幂等：change_id 已存在时 append 变为 upsert（后写覆盖，支持 status 流转
  planned → done → rolled_back）；
- 查询：时间窗 + 服务 + 变更源过滤，时间倒序；
- JSONL 选型：审计友好（git diff 可读）、无外部依赖；量大后可平移到
  SQLite/ES，查询接口不变。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from .schemas import ChangeEvent, Source, parse_ts


class Timeline:
    def __init__(self, path: str):
        self.path = Path(path)
        self._events: dict[str, ChangeEvent] = {}
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                ev = ChangeEvent.from_dict(json.loads(line))
                self._events[ev.change_id] = ev   # 后写覆盖先写

    def upsert(self, event: ChangeEvent) -> bool:
        """写入事件；返回是否为新事件（False = 覆盖已有）。

        注意：更新已有事件时要构造新的 ChangeEvent（from_dict/replace），
        不要原地修改 query 返回的对象——那会让新旧比较恒等而跳过落盘。
        """
        is_new = event.change_id not in self._events
        existing = self._events.get(event.change_id)
        if existing is not None and existing.to_dict() == event.to_dict():
            return False  # 完全相同，不重复落盘
        self._events[event.change_id] = event
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
        return is_new

    def ingest(self, events: list[ChangeEvent]) -> int:
        return sum(1 for ev in events if self.upsert(ev))

    def query(self, since: Optional[str] = None, until: Optional[str] = None,
              service: str = "", source: Optional[Source] = None,
              include_rolled_back: bool = True) -> list[ChangeEvent]:
        s = parse_ts(since) if since else None
        u = parse_ts(until) if until else None
        out = []
        for ev in self._events.values():
            ts = parse_ts(ev.timestamp)
            if s and ts < s:
                continue
            if u and ts > u:
                continue
            if service and ev.service != service:
                continue
            if source and ev.source != source:
                continue
            if not include_rolled_back and ev.status.value == "rolled_back":
                continue
            out.append(ev)
        out.sort(key=lambda e: parse_ts(e.timestamp), reverse=True)
        return out

    def window_before(self, anchor: str, hours: int,
                      service: str = "") -> list[ChangeEvent]:
        """异常关联的标准窗口：[anchor - hours, anchor]。"""
        a = parse_ts(anchor)
        return self.query(since=(a - timedelta(hours=hours)).isoformat(),
                          until=a.isoformat(), service=service)

    def compact(self) -> None:
        """重写文件：每个 change_id 只留最新一行（upsert 追加造成的历史行清掉）。"""
        rows = [json.dumps(ev.to_dict(), ensure_ascii=False)
                for ev in sorted(self._events.values(),
                                 key=lambda e: parse_ts(e.timestamp))]
        self.path.write_text("\n".join(rows) + ("\n" if rows else ""),
                             encoding="utf-8")

    def __len__(self) -> int:
        return len(self._events)
