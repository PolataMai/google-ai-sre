"""时间线存储（幂等/查询/compact）与依赖图（边方向/爆炸半径）。"""
import tempfile
import unittest
from pathlib import Path

from changeflow.deps import ServiceGraph
from changeflow.schemas import Source, Status
from changeflow.timeline import Timeline
from tests import helpers


class TestTimeline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "tl.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def test_upsert_idempotent_and_status_transition(self):
        tl = Timeline(self.path)
        ev = helpers.make_event(change_id="c1")
        self.assertTrue(tl.upsert(ev))
        self.assertFalse(tl.upsert(ev))          # 完全相同不重复
        ev2 = helpers.make_event(change_id="c1", status=Status.ROLLED_BACK)
        self.assertFalse(tl.upsert(ev2))         # 同 id 覆盖，不算新
        # 重新加载后是最新状态（后写覆盖）
        tl2 = Timeline(self.path)
        self.assertEqual(len(tl2), 1)
        self.assertEqual(tl2._events["c1"].status, Status.ROLLED_BACK)

    def test_query_filters_and_order(self):
        tl = Timeline(self.path)
        tl.ingest([
            helpers.make_event("a", timestamp="2026-07-14T10:00:00", service="order"),
            helpers.make_event("b", timestamp="2026-07-14T12:00:00",
                               service="pay", source=Source.CONFIG, details={"keys": ["k"]}),
            helpers.make_event("c", timestamp="2026-07-13T09:00:00", service="order"),
        ])
        got = tl.query(since="2026-07-14T00:00:00")
        self.assertEqual([e.change_id for e in got], ["b", "a"])  # 倒序
        self.assertEqual([e.change_id for e in tl.query(service="order")], ["a", "c"])
        self.assertEqual([e.change_id for e in tl.query(source=Source.CONFIG)], ["b"])

    def test_window_before(self):
        tl = Timeline(self.path)
        tl.ingest([helpers.make_event("in", timestamp="2026-07-14T11:30:00"),
                   helpers.make_event("out", timestamp="2026-07-13T10:00:00")])
        got = tl.window_before("2026-07-14T12:00:00", hours=6)
        self.assertEqual([e.change_id for e in got], ["in"])

    def test_compact_dedupes_file_lines(self):
        tl = Timeline(self.path)
        tl.upsert(helpers.make_event("c1"))
        tl.upsert(helpers.make_event("c1", status=Status.ROLLED_BACK))
        self.assertEqual(len(Path(self.path).read_text().splitlines()), 2)
        tl.compact()
        self.assertEqual(len(Path(self.path).read_text().splitlines()), 1)
        self.assertEqual(Timeline(self.path)._events["c1"].status, Status.ROLLED_BACK)


class TestServiceGraph(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.graph = ServiceGraph.from_malldill_services(
            helpers.write_services_json(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_edges_normalized(self):
        # order 依赖 product（feign），points 依赖 order（mq 消费方依赖生产方）
        self.assertIn("product", self.graph.deps["order"])
        self.assertIn("marketing", self.graph.deps["order"])
        self.assertIn("order", self.graph.deps["points"])

    def test_blast_radius_transitive(self):
        # product 变更 → 直接影响 order/cart，经 order 传导影响 points
        self.assertEqual(self.graph.blast_radius("product"),
                         ["cart", "order", "points"])
        self.assertEqual(self.graph.blast_radius("mall-dill-product-service"),
                         ["cart", "order", "points"])  # 名称归一

    def test_dependencies_of(self):
        self.assertEqual(self.graph.dependencies_of("points"),
                         ["marketing", "order", "product"])


PROJECT_MAP_CALLS = {
    "edges": [
        {"from": "com.malldill.order.service.impl.OrderServiceImpl",
         "to": "com.malldill.product.service.SkuService",
         "from_module": "mall-dill-order", "to_module": "mall-dill-product",
         "count": 12, "cross_module": True, "confidence": "best-effort"},
    ],
    "module_edges": [
        {"from_module": "mall-dill-order", "to_module": "mall-dill-product", "count": 12},
        {"from_module": "mall-dill-cart", "to_module": "mall-dill-product-api", "count": 3},
    ],
}


class TestProjectMapCalls(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.calls_path = str(Path(self.tmp.name) / "calls.json")
        import json
        Path(self.calls_path).write_text(json.dumps(PROJECT_MAP_CALLS),
                                         encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_module_edges_loaded_and_api_normalized(self):
        g = ServiceGraph.from_project_map_calls(self.calls_path)
        self.assertIn("product", g.deps["order"])
        # *-api 契约模块归一到服务本体
        self.assertIn("product", g.deps["cart"])

    def test_fallback_to_class_edges_when_no_module_edges(self):
        import json
        p = Path(self.tmp.name) / "old-calls.json"
        p.write_text(json.dumps({"edges": PROJECT_MAP_CALLS["edges"]}),
                     encoding="utf-8")
        g = ServiceGraph.from_project_map_calls(str(p))
        self.assertIn("product", g.deps["order"])

    def test_merge_fills_in_process_call_blind_spot(self):
        """Feign/MQ 图里 order 不依赖 product（进程内直调盲区）——
        并入 calls 边后补齐，且爆炸半径把 order 及其下游 points 纳入。"""
        base = ServiceGraph.from_malldill_services(
            helpers.write_services_json(self.tmp.name))
        # 夹具的 services.json 里 order 有 ProductApi feign 边；先删掉模拟单体盲区
        base.deps["order"].discard("product")
        base.dependents["product"].discard("order")
        self.assertNotIn("order", base.blast_radius("product"))

        base.merge(ServiceGraph.from_project_map_calls(self.calls_path))
        blast = base.blast_radius("product")
        self.assertIn("order", blast)
        self.assertIn("points", blast)   # 经 order 传导
        self.assertIn("product", base.dependencies_of("order"))


if __name__ == "__main__":
    unittest.main()
