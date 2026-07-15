"""project-map 转换器：symbols/calls → CodeGraph，与内置图合并互补。"""
import tempfile
import unittest

from rca.code_graph import build_from_java_sources
from rca.project_map_adapter import convert, merge
from rca.schemas import ErrorFrame
from tests import helpers

SYMBOLS = [
    {"fqn": "com.example.order.service.PricingService",
     "file": "src/main/java/com/example/order/service/PricingService.java",
     "module": "order-core", "line": 6,
     "methods": [{"name": "applyCoupon", "line": 8}]},
    {"fqn": "com.example.order.service.OrderService",
     "file": "src/main/java/com/example/order/service/OrderService.java",
     "module": "order-core", "line": 6,
     "methods": [{"name": "createOrder", "line": 10}, {"name": "persist", "line": 15}]},
    {"fqn": "com.example.pay.PayFacade",
     "file": "pay/src/main/java/com/example/pay/PayFacade.java",
     "module": "pay-api", "line": 3,
     "methods": [{"name": "pay", "line": 5}]},
]

# project-map calls.json：仅跨模块类级边
CALLS = {"edges": [{
    "from": "com.example.order.service.OrderService",
    "to": "com.example.pay.PayFacade",
    "count": 2, "confidence": "best-effort"}]}


class TestConvert(unittest.TestCase):
    def setUp(self):
        self.graph = convert(SYMBOLS, CALLS)

    def test_method_nodes_with_ranges(self):
        n = self.graph.nodes["com.example.order.service.OrderService.createOrder"]
        self.assertEqual(n.line_start, 10)
        self.assertEqual(n.line_end, 14)   # 下一方法 persist@15 的前一行
        last = self.graph.nodes["com.example.order.service.OrderService.persist"]
        self.assertEqual(last.line_start, 15)
        self.assertGreater(last.line_end, 15)  # 末方法用 span 截断

    def test_class_edges_expanded_to_methods(self):
        callees = self.graph.callees["com.example.order.service.OrderService.createOrder"]
        self.assertIn("com.example.pay.PayFacade.pay", callees)
        # 同类所有方法都连出（过近似，宁多勿漏）
        callees2 = self.graph.callees["com.example.order.service.OrderService.persist"]
        self.assertIn("com.example.pay.PayFacade.pay", callees2)

    def test_resolve_frame(self):
        frame = ErrorFrame(class_fqn="com.example.pay.PayFacade", method="pay",
                           file="PayFacade.java", line=6, is_business=True)
        self.assertEqual(self.graph.resolve_frame(frame),
                         "com.example.pay.PayFacade.pay")

    def test_calls_as_plain_list_supported(self):
        g = convert(SYMBOLS, CALLS["edges"])
        self.assertIn("com.example.pay.PayFacade.pay",
                      g.callees["com.example.order.service.OrderService.createOrder"])


class TestMerge(unittest.TestCase):
    def test_merge_fills_intra_module_edges(self):
        """project-map 缺模块内边，内置解析器缺跨模块边——合并互补。"""
        with tempfile.TemporaryDirectory() as tmp:
            helpers.make_service_repo(tmp)
            builtin = build_from_java_sources(tmp)
        pm = convert(SYMBOLS, CALLS)
        merged = merge(builtin, pm)
        create_order = "com.example.order.service.OrderService.createOrder"
        # 模块内（内置解析器提供）
        self.assertIn("com.example.order.service.PricingService.applyCoupon",
                      merged.callees[create_order])
        # 跨模块（project-map 提供）
        self.assertIn("com.example.pay.PayFacade.pay",
                      merged.callees[create_order])
        # 跨模块可达：从 applyCoupon 上溯到 createOrder 再下钻到 PayFacade
        reach = merged.reachable(
            ["com.example.order.service.PricingService.applyCoupon"])
        self.assertIn("com.example.pay.PayFacade.pay", reach)


if __name__ == "__main__":
    unittest.main()
