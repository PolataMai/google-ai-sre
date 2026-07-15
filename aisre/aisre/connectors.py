"""五类只读连接器与并行上下文收集（F02）。

设计：
- 连接器只读：client 是注入的查询函数 (service, time_range) -> {url, query, snapshot}，
  连接器本身不携带任何写路径——写操作只存在于执行网关（后续迭代）；
- 失败语义分三种：ok / failed（异常，含一次受控重试）/ timeout（超时标缺失，
  不重试——超时源再重试会吃掉整体预算，宁可缺失不阻塞发布）；
- collect_context 用线程池并行调所有源，整体耗时 ≈ 最慢单源，而非各源之和；
- 产出直接是 schemas.Evidence：URL + 查询参数 + 时间范围 + 快照齐备，
  可直接进证据库被事实引用。
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Callable, Optional

from aisre.schemas import Evidence

Client = Callable[[str, tuple[str, str]], dict]


@dataclass
class ConnectorResult:
    source: str
    status: str                       # ok / failed / timeout
    evidences: list[Evidence] = field(default_factory=list)
    error: Optional[str] = None
    attempts: int = 0
    elapsed_ms: float = 0.0


class ReadOnlyConnector:
    """只读连接器基类：一次受控重试；证据 id 以数据源名为前缀命名空间。"""

    source: str = ""

    def __init__(self, client: Client):
        self._client = client

    def collect(self, service: str, time_range: tuple[str, str]) -> ConnectorResult:
        start = time.monotonic()
        attempts = 0
        last_error: Optional[str] = None
        for _ in range(2):                       # 原始调用 + 一次受控重试
            attempts += 1
            try:
                payload = self._client(service, time_range)
                evidence = Evidence(
                    evidence_id=f"{self.source}-{service}-1",
                    source=self.source,
                    query=payload.get("query", ""),
                    time_range=time_range,
                    url=payload.get("url", ""),
                    snapshot=payload.get("snapshot"),
                )
                return ConnectorResult(
                    source=self.source, status="ok", evidences=[evidence],
                    attempts=attempts,
                    elapsed_ms=(time.monotonic() - start) * 1000)
            except Exception as exc:             # noqa: BLE001 —— 数据源故障一律降级为缺失
                last_error = f"{type(exc).__name__}: {exc}"
        return ConnectorResult(
            source=self.source, status="failed", error=last_error,
            attempts=attempts, elapsed_ms=(time.monotonic() - start) * 1000)


class MetricsConnector(ReadOnlyConnector):
    source = "metrics"


class LogsConnector(ReadOnlyConnector):
    source = "logs"


class TraceConnector(ReadOnlyConnector):
    source = "trace"


class ReleaseConnector(ReadOnlyConnector):
    source = "release"


class TopologyConnector(ReadOnlyConnector):
    source = "topology"


def default_connectors(metrics: Client, logs: Client, trace: Client,
                       release: Client, topology: Client) -> list[ReadOnlyConnector]:
    """五类只读连接器，顺序固定：metrics / logs / trace / release / topology。"""
    return [
        MetricsConnector(metrics),
        LogsConnector(logs),
        TraceConnector(trace),
        ReleaseConnector(release),
        TopologyConnector(topology),
    ]


@dataclass
class ContextBundle:
    service: str
    time_range: tuple[str, str]
    results: list[ConnectorResult] = field(default_factory=list)
    evidences: list[Evidence] = field(default_factory=list)

    @property
    def missing_sources(self) -> list[str]:
        return [r.source for r in self.results if r.status != "ok"]


def collect_context(service: str, time_range: tuple[str, str],
                    connectors: list[ReadOnlyConnector],
                    per_source_timeout: float = 40.0) -> ContextBundle:
    """并行查询全部数据源；超时/失败的源标记缺失，不阻塞整体。

    注意：超时源的工作线程无法强杀，只是不再等待其结果——调用方（丰富链路）
    按"80 秒未返回标缺失、90 秒先发布"的预算继续推进。
    """
    bundle = ContextBundle(service=service, time_range=time_range)
    deadline = time.monotonic() + per_source_timeout   # 各源共享同一墙钟截止点
    pool = ThreadPoolExecutor(max_workers=len(connectors))
    futures = {pool.submit(c.collect, service, time_range): c
               for c in connectors}
    for future, conn in futures.items():
        try:
            result = future.result(
                timeout=max(0.0, deadline - time.monotonic()))
        except FutureTimeout:
            result = ConnectorResult(
                source=conn.source, status="timeout",
                error=f"超过 {per_source_timeout}s 未返回，标记缺失")
        bundle.results.append(result)
        bundle.evidences.extend(result.evidences)
    pool.shutdown(wait=False)   # 超时线程自行收尾，不阻塞发布
    return bundle
