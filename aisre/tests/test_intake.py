"""告警接入（F01）：三种 Webhook 格式归一 → 统一 incident_id → 幂等去重 → 启动工作流。

验收对应:
- 统一 incident_id：同一告警指纹永远得到同一 id（确定性,可回放）;
- 幂等：重复投递不重复建事故、不重复触发工作流;
- 未知格式/畸形负载显式报错,不静默吞掉。
"""
import unittest

from aisre.intake import (Alert, IntakeService, MalformedPayload,
                          UnknownFormat, parse_webhook)

AM_PAYLOAD = {
    "receiver": "aisre",
    "status": "firing",
    "alerts": [{
        "status": "firing",
        "fingerprint": "abc123",
        "labels": {"alertname": "HighErrorRate", "service": "payment-api",
                   "severity": "critical"},
        "annotations": {"summary": "error rate > 5%"},
        "startsAt": "2026-07-15T10:08:00Z",
    }],
}

PD_PAYLOAD = {
    "event": {
        "event_type": "incident.triggered",
        "data": {
            "id": "PDINC-77",
            "title": "payment-api latency spike",
            "service": {"summary": "payment-api"},
            "urgency": "high",
            "created_at": "2026-07-15T10:09:00Z",
        },
    },
}

CUSTOM_PAYLOAD = {
    "alert_id": "self-001",
    "service": "payment-api",
    "severity": "P1",
    "title": "连接池耗尽",
    "occurred_at": "2026-07-15T10:10:00Z",
}


class TestParseWebhook(unittest.TestCase):
    def test_alertmanager_format(self):
        alerts = parse_webhook(AM_PAYLOAD, "alertmanager")
        self.assertEqual(len(alerts), 1)
        a = alerts[0]
        self.assertEqual((a.source, a.service, a.severity, a.fingerprint),
                         ("alertmanager", "payment-api", "critical", "abc123"))
        self.assertEqual(a.starts_at, "2026-07-15T10:08:00Z")

    def test_alertmanager_multiple_alerts(self):
        payload = dict(AM_PAYLOAD)
        second = dict(AM_PAYLOAD["alerts"][0], fingerprint="def456")
        payload["alerts"] = [AM_PAYLOAD["alerts"][0], second]
        self.assertEqual(len(parse_webhook(payload, "alertmanager")), 2)

    def test_pagerduty_format(self):
        a = parse_webhook(PD_PAYLOAD, "pagerduty")[0]
        self.assertEqual((a.source, a.service, a.fingerprint),
                         ("pagerduty", "payment-api", "PDINC-77"))

    def test_custom_format(self):
        a = parse_webhook(CUSTOM_PAYLOAD, "custom")[0]
        self.assertEqual((a.source, a.service, a.severity, a.fingerprint),
                         ("custom", "payment-api", "P1", "self-001"))

    def test_unknown_format_raises(self):
        with self.assertRaises(UnknownFormat):
            parse_webhook(AM_PAYLOAD, "zabbix")

    def test_malformed_payload_raises(self):
        with self.assertRaises(MalformedPayload):
            parse_webhook({"alerts": [{"labels": {}}]}, "alertmanager")


class TestIntakeService(unittest.TestCase):
    def setUp(self):
        self.started = []
        self.svc = IntakeService(on_incident=self.started.append)

    def test_new_alert_creates_incident_and_starts_workflow(self):
        results = self.svc.intake(AM_PAYLOAD, "alertmanager")
        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertTrue(r.created)
        self.assertTrue(r.incident_id.startswith("inc-"))
        self.assertEqual(len(self.started), 1)
        self.assertEqual(self.started[0].incident_id, r.incident_id)

    def test_duplicate_delivery_is_idempotent(self):
        first = self.svc.intake(AM_PAYLOAD, "alertmanager")[0]
        second = self.svc.intake(AM_PAYLOAD, "alertmanager")[0]
        self.assertEqual(second.incident_id, first.incident_id)
        self.assertFalse(second.created)
        self.assertEqual(len(self.started), 1)   # 工作流只触发一次

    def test_incident_id_is_deterministic_across_instances(self):
        other = IntakeService()
        a = self.svc.intake(AM_PAYLOAD, "alertmanager")[0]
        b = other.intake(AM_PAYLOAD, "alertmanager")[0]
        self.assertEqual(a.incident_id, b.incident_id)

    def test_different_fingerprints_get_different_incidents(self):
        a = self.svc.intake(AM_PAYLOAD, "alertmanager")[0]
        b = self.svc.intake(CUSTOM_PAYLOAD, "custom")[0]
        self.assertNotEqual(a.incident_id, b.incident_id)
        self.assertEqual(len(self.started), 2)
