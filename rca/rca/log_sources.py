"""日志源 adapter：把不同日志系统的输出归一为线 1 解析器可消费的文本。

三种来源，契约相同（返回 (日志文本, 警告列表)）：
- read_files    ：本地/挂载的日志文件（原有行为）；
- read_command  ：任意导出命令（SLS CLI、kubectl logs、logcli…）的 stdout；
- fetch_elasticsearch：ES `_search`（urllib 零依赖），按告警时间开窗查询
  ERROR/FATAL 文档，并将 @timestamp/level/message/stack_trace 字段重组为
  "时间 级别 消息 + 堆栈" 的行格式——解析器不感知来源差异。

时区约定：ES 的 @timestamp 为 UTC，重组后直接进入统一的 UTC-naive 管道。
"""
from __future__ import annotations

import base64
import json
import subprocess
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path


def read_files(paths: list[str]) -> tuple[str, list[str]]:
    text = "\n".join(
        Path(p).read_text(encoding="utf-8", errors="replace") for p in paths)
    return text, []


def read_command(cmd: str | list[str], timeout: int = 120) -> tuple[str, list[str]]:
    """执行导出命令取日志（如 `kubectl logs`、SLS/logcli 查询命令）。

    信任边界：cmd 只能来自操作者本人的 CLI 参数（等价于其在自己 shell 里执行），
    **绝不允许**由告警内容、日志内容等外部数据拼接而成——那是注入面。
    程序化调用请传 list 形式（不经 shell）；str 形式仅供 CLI 直通操作者输入，
    需要管道/重定向时才使用。
    """
    use_shell = isinstance(cmd, str)
    proc = subprocess.run(cmd, shell=use_shell, capture_output=True,
                          text=True, timeout=timeout)
    warnings = []
    if proc.returncode != 0:
        warnings.append(f"日志导出命令退出码 {proc.returncode}：{proc.stderr.strip()[:200]}")
    return proc.stdout, warnings


@dataclass
class EsConfig:
    url: str                      # http://es-host:9200
    index: str                    # app-log-*
    time_from: str                # ISO，UTC
    time_to: str
    service: str = ""             # 结合 service_field 过滤
    service_field: str = ""
    query: str = ""               # 额外 query_string
    timestamp_field: str = "@timestamp"
    level_field: str = "level"    # 置空则不过滤级别
    message_field: str = "message"
    stack_field: str = "stack_trace"
    size: int = 2000
    auth: str = ""                # "user:pass" → Basic


def build_es_body(cfg: EsConfig) -> dict:
    filters: list[dict] = [{"range": {cfg.timestamp_field: {
        "gte": cfg.time_from, "lte": cfg.time_to}}}]
    if cfg.level_field:
        filters.append({"terms": {cfg.level_field: ["ERROR", "FATAL"]}})
    if cfg.service and cfg.service_field:
        filters.append({"term": {cfg.service_field: cfg.service}})
    if cfg.query:
        filters.append({"query_string": {"query": cfg.query}})
    return {"size": cfg.size,
            "sort": [{cfg.timestamp_field: "asc"}],
            "query": {"bool": {"filter": filters}}}


def _rebuild_lines(hits: list[dict], cfg: EsConfig) -> str:
    """把 ES 文档重组为解析器认识的 `时间 级别 消息（+堆栈）` 文本。"""
    out: list[str] = []
    for h in hits:
        src = h.get("_source", {})
        ts = str(src.get(cfg.timestamp_field, "")).strip()
        level = str(src.get(cfg.level_field, "ERROR") or "ERROR").strip()
        msg = str(src.get(cfg.message_field, "")).strip()
        out.append(f"{ts} {level} {msg}")
        stack = str(src.get(cfg.stack_field, "") or "").strip()
        if stack:
            out.append(stack)
    return "\n".join(out)


def fetch_elasticsearch(cfg: EsConfig) -> tuple[str, list[str]]:
    body = json.dumps(build_es_body(cfg)).encode()
    req = urllib.request.Request(
        f"{cfg.url.rstrip('/')}/{cfg.index}/_search",
        data=body, method="POST",
        headers={"Content-Type": "application/json"})
    if cfg.auth:
        token = base64.b64encode(cfg.auth.encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())

    hits = data.get("hits", {})
    docs = hits.get("hits", [])
    total = hits.get("total", 0)
    if isinstance(total, dict):
        total = total.get("value", 0)
    warnings = []
    if total > len(docs):
        warnings.append(
            f"ES 命中 {total} 条但只取回 {len(docs)} 条（size={cfg.size}）——"
            f"错误量级统计可能偏小，必要时缩小时间窗或加大 size")
    return _rebuild_lines(docs, cfg), warnings
