"""日志源 adapter：ES 查询构造/行重组（本地 http 桩）、命令源、文件源。"""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from rca.log_forensics import analyze_log
from rca.log_sources import EsConfig, build_es_body, fetch_elasticsearch, read_command

ES_DOCS = [
    {"_source": {
        "@timestamp": "2026-07-11T14:23:05.123Z", "level": "ERROR",
        "service": "order-service",
        "message": "create order failed",
        "stack_trace": ('java.lang.NullPointerException: because "coupon" is null\n'
                        "\tat com.example.order.service.PricingService.applyCoupon(PricingService.java:9)\n"
                        "\tat java.base/java.lang.Thread.run(Thread.java:833)")}},
    {"_source": {
        "@timestamp": "2026-07-11T14:24:00.000Z", "level": "ERROR",
        "service": "order-service",
        "message": "create order failed",
        "stack_trace": ('java.lang.NullPointerException: because "coupon" is null\n'
                        "\tat com.example.order.service.PricingService.applyCoupon(PricingService.java:9)")}},
]


class _Handler(BaseHTTPRequestHandler):
    captured = {}

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        _Handler.captured = {
            "path": self.path,
            "body": json.loads(self.rfile.read(length).decode()),
            "auth": self.headers.get("Authorization", ""),
        }
        payload = json.dumps({
            "hits": {"total": {"value": 5}, "hits": ES_DOCS}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):  # 静音
        pass


class TestElasticsearch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def _cfg(self) -> EsConfig:
        return EsConfig(
            url=f"http://127.0.0.1:{self.port}", index="app-log-2026.07.11",
            time_from="2026-07-11T12:25:00", time_to="2026-07-11T14:25:00",
            service="order-service", service_field="service",
            auth="elastic:secret", size=100)

    def test_query_body_and_auth(self):
        fetch_elasticsearch(self._cfg())
        cap = _Handler.captured
        self.assertEqual(cap["path"], "/app-log-2026.07.11/_search")
        self.assertTrue(cap["auth"].startswith("Basic "))
        filters = cap["body"]["query"]["bool"]["filter"]
        kinds = [next(iter(f)) for f in filters]
        self.assertIn("range", kinds)
        self.assertIn("terms", kinds)   # level ERROR/FATAL
        self.assertIn("term", kinds)    # service 过滤
        rng = next(f for f in filters if "range" in f)["range"]["@timestamp"]
        self.assertEqual(rng["gte"], "2026-07-11T12:25:00")

    def test_rebuilt_lines_feed_parser(self):
        """重组文本可直接进线 1 解析器并得到正确聚类。"""
        text, warnings = fetch_elasticsearch(self._cfg())
        sigs = analyze_log(text, "order-service", ["com.example"])
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0].count, 2)
        self.assertEqual(sigs[0].top_business_frame.method, "applyCoupon")
        # first_seen 来自 @timestamp（UTC）
        self.assertTrue(sigs[0].first_seen.startswith("2026-07-11T14:23:05"))

    def test_truncation_warning(self):
        _, warnings = fetch_elasticsearch(self._cfg())
        self.assertTrue(any("只取回" in w for w in warnings), warnings)

    def test_body_skips_optional_filters(self):
        cfg = self._cfg()
        cfg.level_field = ""
        cfg.service_field = ""
        body = build_es_body(cfg)
        kinds = [next(iter(f)) for f in body["query"]["bool"]["filter"]]
        self.assertEqual(kinds, ["range"])


class TestCommandSource(unittest.TestCase):
    def test_list_form_no_shell(self):
        text, warnings = read_command(["echo", "hello $HOME"])
        self.assertEqual(text.strip(), "hello $HOME")  # 未经 shell 展开
        self.assertEqual(warnings, [])

    def test_failure_produces_warning(self):
        _, warnings = read_command(["false"])
        self.assertTrue(warnings and "退出码" in warnings[0])


if __name__ == "__main__":
    unittest.main()
