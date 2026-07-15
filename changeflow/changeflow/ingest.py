"""五源 ingestor：把各系统的变更记录归一为 ChangeEvent。

- from_git         代码提交（原料层；发布单用 from_events_json 声明灰度/回滚）
- from_rca_audit   rca 生态的 audit.json（Apollo/Nacos/DDL 经 rca audit-convert 的产物直接吃）
- from_events_json 发布系统/中间件/基础设施的通用导出（完整 ChangeEvent 字段）
"""
from __future__ import annotations

import json
import subprocess
from datetime import timedelta
from pathlib import Path

from .schemas import ChangeEvent, Source, Status, parse_ts


def _git(repo: str, *args: str) -> str:
    return subprocess.run(["git", "-C", repo, *args],
                          capture_output=True, text=True, check=True).stdout


def from_git(repo: str, service: str, since: str, until: str = "",
             ref: str = "HEAD") -> list[ChangeEvent]:
    """git 提交 → code 变更事件（含 touched files，供范围因子评分）。"""
    args = ["log", ref, f"--since={parse_ts(since).isoformat()}+00:00",
            "--pretty=format:%H%x1f%aI%x1f%an%x1f%s"]
    if until:
        args.insert(2, f"--until={parse_ts(until).isoformat()}+00:00")
    events = []
    for row in _git(repo, *args).splitlines():
        if not row.strip():
            continue
        sha, ts, author, subject = row.split("\x1f", 3)
        files = [l for l in _git(repo, "show", "--name-only",
                                 "--pretty=format:", sha).splitlines() if l.strip()]
        events.append(ChangeEvent(
            change_id=f"git-{sha[:12]}", source=Source.CODE, service=service,
            timestamp=ts, summary=subject, author=author,
            details={"commit": sha, "files": files}))
    return events


_AUDIT_TYPE_MAP = {"config": Source.CONFIG, "db": Source.DB, "infra": Source.INFRA}


def from_rca_audit(path: str) -> list[ChangeEvent]:
    """rca audit.json 契约（id/type/service/timestamp/summary/author/keys/relation）。"""
    entries = json.loads(Path(path).read_text(encoding="utf-8"))
    events = []
    for e in entries:
        src = _AUDIT_TYPE_MAP.get(e.get("type", ""), Source.INFRA)
        details = {"keys": list(e.get("keys", []))}
        if src == Source.DB:
            details = {"tables": list(e.get("keys", [])), "ddl": True}
        if e.get("relation"):
            details["relation"] = e["relation"]
        events.append(ChangeEvent(
            change_id=e["id"], source=src, service=e.get("service", ""),
            timestamp=e["timestamp"], summary=e.get("summary", ""),
            author=e.get("author", ""), details=details))
    return events


def from_events_json(path: str) -> list[ChangeEvent]:
    """通用导出：完整 ChangeEvent 字段（发布单可声明 gray/rollback_plan/status）。"""
    entries = json.loads(Path(path).read_text(encoding="utf-8"))
    return [ChangeEvent.from_dict(e) for e in entries]
