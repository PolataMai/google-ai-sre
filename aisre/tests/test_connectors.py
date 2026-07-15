"""五类只读连接器与并行上下文收集（F02）。

验收对应:
- 五类数据源:metrics / logs / trace / release / topology;
- 全部并行调用,单个数据源失败不阻塞整体;
- 每源一次受控重试(对异常);超时标记缺失,不重试、不阻塞;
- 产出的证据带 URL、查询参数、时间范围、快照,可直接进证据库。
"""
import time
import unittest

from aisre.connectors import (ConnectorResult, ContextBundle,
                              MetricsConnector, LogsConnector, TraceConnector,
                              ReleaseConnector, TopologyConnector,
                              collect_context, default_connectors)

WINDOW = ("2026-07-15T10:00:00Z", "2026-07-15T10:15:00Z")


def ok_client(payload):
    def fetch(service, time_range):
        return {"url": f"https://src.example.com/q?svc={service}",
                "query": f"query({service})", "snapshot": payload}
    return fetch


class FlakyClient:
    """第一次抛异常,第二次成功——验证一次受控重试。"""
    def __init__(self):
        self.calls = 0

    def __call__(self, service, time_range):
        self.calls += 1
        if self.calls == 1:
            raise ConnectionError("暂时不可用")
        return {"url": "https://src/q", "query": "q", "snapshot": {"ok": 1}}


def always_fail(service, time_range):
    raise ConnectionError("持续不可用")


def slow_client(service, time_range):
    time.sleep(0.5)
    return {"url": "https://slow/q", "query": "q", "snapshot": {}}


class TestSingleConnector(unittest.TestCase):
    def test_five_connector_sources(self):
        connectors = default_connectors(
            metrics=ok_client({"error_rate": 0.08}),
            logs=ok_client({"lines": 12}),
            trace=ok_client({"spans": 3}),
            release=ok_client({"version": "v42"}),
            topology=ok_client({"deps": ["db"]}),
        )
        self.assertEqual([c.source for c in connectors],
                         ["metrics", "logs", "trace", "release", "topology"])

    def test_connector_produces_complete_evidence(self):
        conn = MetricsConnector(client=ok_client({"error_rate": 0.08}))
        result = conn.collect("payment-api", WINDOW)
        self.assertEqual(result.status, "ok")
        ev = result.evidences[0]
        self.assertEqual(ev.source, "metrics")
        self.assertTrue(ev.evidence_id.startswith("metrics-"))
        self.assertEqual(ev.time_range, WINDOW)
        self.assertTrue(ev.url)
        self.assertTrue(ev.query)
        self.assertEqual(ev.snapshot, {"error_rate": 0.08})

    def test_one_controlled_retry_on_exception(self):
        client = FlakyClient()
        result = LogsConnector(client=client).collect("payment-api", WINDOW)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.attempts, 2)
        self.assertEqual(client.calls, 2)

    def test_persistent_failure_marks_failed_after_retry(self):
        result = TraceConnector(client=always_fail).collect("payment-api", WINDOW)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.attempts, 2)   # 原始调用 + 一次重试,不再多试
        self.assertIn("持续不可用", result.error)


class TestParallelCollect(unittest.TestCase):
    def _connectors(self, **overrides):
        clients = {
            "metrics": ok_client({"error_rate": 0.08}),
            "logs": ok_client({"lines": 12}),
            "trace": ok_client({"spans": 3}),
            "release": ok_client({"version": "v42"}),
            "topology": ok_client({"deps": ["db"]}),
        }
        clients.update(overrides)
        return default_connectors(**clients)

    def test_all_sources_ok(self):
        bundle = collect_context("payment-api", WINDOW, self._connectors())
        self.assertEqual(bundle.missing_sources, [])
        self.assertEqual(len(bundle.evidences), 5)
        self.assertEqual({r.source for r in bundle.results},
                         {"metrics", "logs", "trace", "release", "topology"})

    def test_single_failure_does_not_block_others(self):
        bundle = collect_context("payment-api", WINDOW,
                                 self._connectors(logs=always_fail))
        self.assertEqual(bundle.missing_sources, ["logs"])
        self.assertEqual(len(bundle.evidences), 4)   # 其余四源正常产出
        by_source = {r.source: r for r in bundle.results}
        self.assertEqual(by_source["logs"].status, "failed")
        self.assertEqual(by_source["metrics"].status, "ok")

    def test_timeout_marks_source_missing(self):
        bundle = collect_context("payment-api", WINDOW,
                                 self._connectors(trace=slow_client),
                                 per_source_timeout=0.1)
        by_source = {r.source: r for r in bundle.results}
        self.assertEqual(by_source["trace"].status, "timeout")
        self.assertIn("trace", bundle.missing_sources)
        self.assertEqual(len(bundle.evidences), 4)

    def test_sources_run_in_parallel_not_serial(self):
        # 五个 0.2s 的源并行应远小于串行 1.0s
        def sleepy(service, time_range):
            time.sleep(0.2)
            return {"url": "https://s/q", "query": "q", "snapshot": {}}
        start = time.monotonic()
        collect_context("payment-api", WINDOW,
                        self._connectors(**{k: sleepy for k in
                                            ("metrics", "logs", "trace",
                                             "release", "topology")}),
                        per_source_timeout=2)
        self.assertLess(time.monotonic() - start, 0.8)

    def test_bundle_reports_evidence_ids_unique(self):
        bundle = collect_context("payment-api", WINDOW, self._connectors())
        ids = [e.evidence_id for e in bundle.evidences]
        self.assertEqual(len(ids), len(set(ids)))
