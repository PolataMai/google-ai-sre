"""Code graph：Java 源码解析、调用边、可达集、JSON 契约。"""
import tempfile
import unittest

from rca.code_graph import CodeGraph, build_from_java_sources
from rca.schemas import ErrorFrame, TouchedRegion
from tests import helpers

PRICING = "com.example.order.service.PricingService.applyCoupon"
CREATE_ORDER = "com.example.order.service.OrderService.createOrder"
PERSIST = "com.example.order.service.OrderService.persist"
CONTROLLER = "com.example.order.controller.OrderController.create"
GET_DISCOUNT = "com.example.order.model.Coupon.getDiscount"
CHARGE = "com.example.order.gateway.PaymentClient.charge"


class TestBuild(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.info = helpers.make_service_repo(cls.tmp.name)
        cls.graph = build_from_java_sources(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_nodes_registered(self):
        for nid in (PRICING, CREATE_ORDER, PERSIST, CONTROLLER, GET_DISCOUNT, CHARGE):
            self.assertIn(nid, self.graph.nodes, nid)

    def test_call_edges(self):
        self.assertIn(PRICING, self.graph.callees[CREATE_ORDER])
        self.assertIn(PERSIST, self.graph.callees[CREATE_ORDER])
        self.assertIn(CREATE_ORDER, self.graph.callees[CONTROLLER])
        self.assertIn(GET_DISCOUNT, self.graph.callees[PRICING])
        # 反向索引
        self.assertIn(CREATE_ORDER, self.graph.callers[PRICING])

    def test_reachable_set(self):
        reach = self.graph.reachable([PRICING])
        # 上溯：调用方链路全部可达
        self.assertIn(CREATE_ORDER, reach)
        self.assertIn(CONTROLLER, reach)
        # 下钻：被调方可达
        self.assertIn(GET_DISCOUNT, reach)
        # 无关的支付客户端不可达
        self.assertNotIn(CHARGE, reach)

    def test_resolve_frame_and_nodes_touching(self):
        frame = ErrorFrame(class_fqn="com.example.order.service.PricingService",
                           method="applyCoupon", file="PricingService.java",
                           line=self.info["npe_line"], is_business=True)
        self.assertEqual(self.graph.resolve_frame(frame), PRICING)
        region = TouchedRegion(file=f"{helpers.SRC}/service/PricingService.java",
                               line_start=self.info["npe_line"],
                               line_end=self.info["npe_line"])
        hits = [n.id for n in self.graph.nodes_touching(region)]
        self.assertEqual(hits, [PRICING])

    def test_json_round_trip(self):
        g2 = CodeGraph.from_json(self.graph.to_json())
        self.assertEqual(set(g2.nodes), set(self.graph.nodes))
        self.assertEqual(
            {(a, b) for a, bs in g2.callees.items() for b in bs},
            {(a, b) for a, bs in self.graph.callees.items() for b in bs})


if __name__ == "__main__":
    unittest.main()
