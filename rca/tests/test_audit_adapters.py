"""审计 adapter：Apollo/Nacos/DDL 导出 → audit.json 契约 → 能被线 2 消费。"""
import json
import tempfile
import unittest
from pathlib import Path

from rca.audit_adapters import from_apollo, from_ddl, from_nacos
from rca.change_sources import collect_audit_changes
from rca.schemas import ChangeType

APOLLO = [{
    "id": 4102, "appId": "order-svc", "clusterName": "default",
    "namespaceName": "application", "name": "20260711140000-release",
    "comment": "调低券服务读取超时",
    "configurations": {"coupon.read.timeout.ms": "200", "feature.x": "on"},
    "dataChangeCreatedBy": "ops-a",
    "dataChangeCreatedTime": "2026-07-11T13:00:00+00:00",
}]

NACOS = [{
    "id": "77", "dataId": "order-service-prod.yaml", "group": "DEFAULT_GROUP",
    "appName": "", "srcUser": "ops-b", "opType": "U ",
    "lastModifiedTime": "2026-07-11T12:30:00+00:00",
}]

DDL = [{
    "ticket_id": "D-901", "database": "order_db", "tables": ["t_order_coupon"],
    "summary": "t_order_coupon 加索引", "executed_at": "2026-07-11T11:00:00+00:00",
    "executor": "dba",
}]


class TestConverters(unittest.TestCase):
    def test_apollo_mapping_with_service_map(self):
        out = from_apollo(APOLLO, service_map={"order-svc": "order-service"})
        e = out[0]
        self.assertEqual(e["id"], "apollo-4102")
        self.assertEqual(e["type"], "config")
        self.assertEqual(e["service"], "order-service")
        self.assertEqual(e["keys"], ["coupon.read.timeout.ms", "feature.x"])
        self.assertEqual(e["summary"], "调低券服务读取超时")

    def test_nacos_service_from_dataid(self):
        e = from_nacos(NACOS)[0]
        self.assertEqual(e["service"], "order-service-prod")
        self.assertEqual(e["keys"], ["order-service-prod.yaml"])
        self.assertIn("Nacos U", e["summary"])

    def test_ddl_without_service_marks_shared_db(self):
        e = from_ddl(DDL)[0]
        self.assertEqual(e["type"], "db")
        self.assertEqual(e["relation"], "shared-db")
        self.assertIn("t_order_coupon", e["keys"])

    def test_converted_entries_flow_into_line2(self):
        """转换产物直接进线 2：窗口过滤 + 类型/关联路径正确。"""
        entries = (from_apollo(APOLLO, {"order-svc": "order-service"})
                   + from_nacos(NACOS) + from_ddl(DDL))
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "audit.json"
            p.write_text(json.dumps(entries), encoding="utf-8")
            changes = collect_audit_changes(
                str(p), "order-service", "2026-07-11 14:23:05.123", 72)
        by_id = {c.change_id: c for c in changes}
        self.assertEqual(len(changes), 3)
        self.assertEqual(by_id["apollo-4102"].relation, "same-service")
        self.assertEqual(by_id["ddl-D-901"].relation, "shared-db")
        self.assertEqual(by_id["ddl-D-901"].change_type, ChangeType.DB)
        self.assertEqual(by_id["nacos-77"].relation, "other-service")


if __name__ == "__main__":
    unittest.main()
