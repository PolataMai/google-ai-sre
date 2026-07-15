"""线 2：变更取证。

以"错误首次出现时间"为锚点向前开窗，从多个变更源收集候选变更：
- GitChangeSource：代码变更（commit + diff hunk → TouchedRegion，变更后行号区间）；
- AuditChangeSource：配置/DB/基础设施变更（JSON 审计文件适配器，
  生产上可替换为 Apollo/Nacos 审计接口、DDL 工单系统的适配器，契约不变）。
"""
from __future__ import annotations

import json
import subprocess
from datetime import timedelta
from pathlib import Path
from typing import Optional

from .schemas import CandidateChange, ChangeType, TouchedRegion, parse_ts

HUNK_RE_PREFIX = "@@ "


def _git(repo: str, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True, text=True, check=True)
    return out.stdout


def _parse_hunks(diff_text: str) -> list[TouchedRegion]:
    """解析 --unified=0 diff，产出变更后版本的行号区间。"""
    regions: list[TouchedRegion] = []
    current_file = ""
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[len("+++ b/"):]
        elif line.startswith("+++ /dev/null"):
            current_file = ""  # 文件被删除：新版本没有行号可交
        elif line.startswith(HUNK_RE_PREFIX) and current_file:
            # @@ -a,b +c,d @@ optional function context
            try:
                header, _, context = line[3:].partition(" @@")
                new_part = header.split(" ")[1]  # +c,d 或 +c
                nums = new_part.lstrip("+").split(",")
                start = int(nums[0])
                count = int(nums[1]) if len(nums) > 1 else 1
            except (IndexError, ValueError):
                continue
            if count == 0:
                # 纯删除 hunk：新版本无对应行，但删除行为本身可能是根因，
                # 记为落点前一行的零宽区域，供符号级匹配
                regions.append(TouchedRegion(
                    file=current_file, line_start=max(start, 1),
                    line_end=max(start, 1), symbol_hint=context.strip()))
            else:
                regions.append(TouchedRegion(
                    file=current_file, line_start=start,
                    line_end=start + count - 1, symbol_hint=context.strip()))
    return regions


def collect_git_changes(repo: str, service: str, anchor_time: str,
                        window_hours: int = 72,
                        ref: str = "HEAD",
                        relation: str = "same-service") -> list[CandidateChange]:
    """收集 [anchor - window, anchor] 内、ref 可达的代码变更。

    relation：本服务仓库为 same-service；上游服务/共享库仓库传 upstream/shared-lib。
    """
    anchor = parse_ts(anchor_time)
    since = anchor - timedelta(hours=window_hours)
    # 内部时间为 UTC-naive，显式带 +00:00 传给 git，避免被按本机时区解释
    log = _git(repo, "log", ref,
               f"--since={since.isoformat()}+00:00",
               f"--until={anchor.isoformat()}+00:00",
               "--date=iso-strict", "--pretty=format:%H%x1f%aI%x1f%an%x1f%s")
    changes: list[CandidateChange] = []
    for row in log.splitlines():
        if not row.strip():
            continue
        sha, ts, author, subject = row.split("\x1f", 3)
        diff = _git(repo, "show", "--unified=0", "--pretty=format:", sha)
        changes.append(CandidateChange(
            change_id=sha, change_type=ChangeType.CODE, service=service,
            timestamp=ts, summary=subject, author=author,
            relation=relation, touched=_parse_hunks(diff)))
    changes.sort(key=lambda c: parse_ts(c.timestamp), reverse=True)
    return changes


def collect_audit_changes(audit_path: str, service: str, anchor_time: str,
                          window_hours: int = 72) -> list[CandidateChange]:
    """从 JSON 审计文件收集配置/DB/基础设施变更。

    审计条目格式：
    {"id": "...", "type": "config|db|infra", "service": "...",
     "timestamp": "...", "summary": "...", "author": "...",
     "keys": ["timeout.ms", "t_order"], "relation": "shared-db"(可选)}
    """
    anchor = parse_ts(anchor_time)
    since = anchor - timedelta(hours=window_hours)
    entries = json.loads(Path(audit_path).read_text(encoding="utf-8"))
    changes: list[CandidateChange] = []
    for e in entries:
        ts = parse_ts(e["timestamp"])
        if not (since <= ts <= anchor):
            continue  # 锚点之后的变更不可能是本次故障的原因
        relation = e.get("relation") or (
            "same-service" if e.get("service") == service else "other-service")
        changes.append(CandidateChange(
            change_id=e["id"], change_type=ChangeType(e["type"]),
            service=e.get("service", ""), timestamp=e["timestamp"],
            summary=e.get("summary", ""), author=e.get("author", ""),
            relation=relation, keys=list(e.get("keys", []))))
    changes.sort(key=lambda c: parse_ts(c.timestamp), reverse=True)
    return changes


def resolve_repo_path(repo: str, commit: str, file_basename: str) -> Optional[str]:
    """按文件名后缀在指定 commit 的文件树里解析仓库相对路径（堆栈帧只有基名）。"""
    tree = _git(repo, "ls-tree", "-r", "--name-only", commit)
    matches = [p for p in tree.splitlines() if p.endswith("/" + file_basename) or p == file_basename]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        return None
    # 多个同名文件：无法唯一定位时返回 None，让上层降级为符号匹配而不是猜一个
    return None


def read_file_at(repo: str, commit: str, path: str) -> Optional[str]:
    try:
        return _git(repo, "show", f"{commit}:{path}")
    except subprocess.CalledProcessError:
        return None
