"""线 1：堆栈解析、框架帧过滤、Caused by 折叠、指纹聚类。"""
import unittest

from rca.log_forensics import analyze_log, demultiplex, parse_error_events
from tests import helpers


def _fake_info():
    return {"npe_line": 9, "os_call_line": 11, "ctrl_call_line": 12, "charge_line": 5}


class TestParse(unittest.TestCase):
    def setUp(self):
        self.text = helpers.make_incident_log(_fake_info())

    def test_extracts_all_error_events(self):
        events = parse_error_events(self.text, "order-service", ["com.example"])
        self.assertEqual(len(events), 4)  # 3 次 NPE + 1 次超时

    def test_frames_and_business_classification(self):
        ev = parse_error_events(self.text, "order-service", ["com.example"])[0]
        self.assertEqual(ev.exception_type, "java.lang.NullPointerException")
        self.assertEqual(len(ev.frames), 5)
        business = [f for f in ev.frames if f.is_business]
        self.assertEqual(len(business), 3)
        top = business[0]
        self.assertEqual(top.class_fqn, "com.example.order.service.PricingService")
        self.assertEqual(top.method, "applyCoupon")
        self.assertEqual(top.file, "PricingService.java")
        self.assertEqual(top.line, 9)

    def test_java_module_prefix_stripped(self):
        ev = parse_error_events(self.text, "order-service", ["com.example"])[0]
        thread_frame = ev.frames[-1]
        self.assertEqual(thread_frame.class_fqn, "java.lang.Thread")
        self.assertFalse(thread_frame.is_business)

    def test_caused_by_uses_deepest_cause(self):
        text = "\n".join([
            "2026-07-11 14:30:00.000 ERROR [order-service] h : batch failed",
            "java.lang.RuntimeException: wrapper",
            "\tat com.example.order.service.OrderService.createOrder(OrderService.java:11)",
            "Caused by: java.sql.SQLException: Connection refused",
            "\tat com.example.order.repo.OrderRepo.save(OrderRepo.java:20)",
            "\t... 12 common frames omitted",
        ])
        events = parse_error_events(text, "order-service", ["com.example"])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].exception_type, "java.sql.SQLException")
        self.assertEqual(events[0].frames[0].method, "save")


class TestCluster(unittest.TestCase):
    def test_cluster_and_first_seen(self):
        sigs = analyze_log(helpers.make_incident_log(_fake_info()),
                           "order-service", ["com.example"])
        self.assertEqual(len(sigs), 2)
        npe = next(s for s in sigs if "NullPointer" in s.exception_type)
        timeout = next(s for s in sigs if "Timeout" in s.exception_type)
        self.assertEqual(npe.count, 3)
        self.assertEqual(npe.first_seen, helpers.NPE_FIRST_SEEN)
        self.assertEqual(npe.last_seen, "2026-07-11 14:24:30.000")
        self.assertEqual(npe.top_business_frame.symbol,
                         "com.example.order.service.PricingService.applyCoupon")
        self.assertEqual(timeout.count, 1)
        self.assertEqual(timeout.top_business_frame.method, "charge")

    def test_fingerprint_stable_across_line_drift(self):
        """行号漂移（版本变化）不改变指纹——指纹只含符号不含行号。"""
        a = _fake_info()
        b = dict(a, npe_line=12, os_call_line=14)  # 同一错误在另一版本的行号
        sig_a = analyze_log(helpers.make_incident_log(a), "s", ["com.example"])[0]
        sig_b = analyze_log(helpers.make_incident_log(b), "s", ["com.example"])[0]
        self.assertEqual(sig_a.fingerprint, sig_b.fingerprint)

    def test_no_business_packages_falls_back_to_framework_filter(self):
        sigs = analyze_log(helpers.make_incident_log(_fake_info()), "order-service")
        npe = next(s for s in sigs if "NullPointer" in s.exception_type)
        self.assertEqual(npe.top_business_frame.method, "applyCoupon")


class TestDemultiplex(unittest.TestCase):
    """kubectl --prefix 式多流交织：两个 pod 的堆栈逐行穿插。"""

    @staticmethod
    def _interleaved() -> str:
        a = [
            "2026-07-11 14:23:05.123 ERROR [order-service] h : create order failed",
            "java.lang.NullPointerException: a",
            "\tat com.example.order.service.PricingService.applyCoupon(PricingService.java:9)",
            "\tat com.example.order.service.OrderService.createOrder(OrderService.java:11)",
        ]
        b = [
            "2026-07-11 14:23:06.000 ERROR [order-service] h : pay failed",
            "java.net.SocketTimeoutException: Read timed out",
            "\tat com.example.order.gateway.PaymentClient.charge(PaymentClient.java:5)",
            "\tat java.base/java.lang.Thread.run(Thread.java:833)",
        ]
        lines = []
        for la, lb in zip(a, b):  # 逐行穿插，模拟两个 pod 同时输出
            lines.append(f"[pod/order-aaa] {la}")
            lines.append(f"[pod/order-bbb] {lb}")
        return "\n".join(lines)

    def test_demultiplex_splits_and_strips_prefix(self):
        streams = demultiplex(self._interleaved(), r"^\[pod/([^\]]+)\]\s?")
        self.assertEqual(len(streams), 2)
        self.assertTrue(all("[pod/" not in s for s in streams))

    def test_interleaved_parsing_recovers_both_stacks(self):
        text = self._interleaved()
        sigs = analyze_log(text, "order-service", ["com.example"],
                           stream_re=r"^\[pod/([^\]]+)\]\s?")
        by_exc = {s.exception_type: s for s in sigs}
        self.assertEqual(len(sigs), 2)
        self.assertEqual(
            by_exc["java.lang.NullPointerException"].top_business_frame.method,
            "applyCoupon")
        self.assertEqual(
            by_exc["java.net.SocketTimeoutException"].top_business_frame.method,
            "charge")
        # 不拆流直接解析会把 A 流的帧挂到 B 流异常上（对照组说明拆流必要性）
        naive = analyze_log(text, "order-service", ["com.example"])
        naive_npe = next((s for s in naive
                          if s.exception_type == "java.lang.NullPointerException"), None)
        self.assertTrue(
            naive_npe is None
            or [f.method for f in naive_npe.frames]
            != [f.method for f in by_exc["java.lang.NullPointerException"].frames])

    def test_unprefixed_lines_stay_with_previous_stream(self):
        text = ("[pod/x] 2026-07-11 14:23:05.123 ERROR h : boom\n"
                "[pod/x] java.lang.IllegalStateException: s\n"
                "\tat com.example.a.B.c(B.java:3)\n")  # 该行无前缀
        sigs = analyze_log(text, "s", ["com.example"],
                           stream_re=r"^\[pod/([^\]]+)\]\s?")
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0].top_business_frame.method, "c")


if __name__ == "__main__":
    unittest.main()
