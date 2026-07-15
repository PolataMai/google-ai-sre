"""90 天基线：从历史事故与变更记录计算试点对照基线。

统一口径（与指标看板一致，全部可从记录重算）：
- MTTM = mitigated_at - started_at，分钟；
- 中位数与 p75 用 nearest-rank（确定性，便于审计重算）；
- 只统计窗口内(started_at ∈ [as_of-window, as_of])的事故；
- 未缓解（open）的事故不进 MTTM，单独计数；
- 变更失败率 = 窗口内 failed 变更数 ÷ 窗口内变更总数。
"""
import json
import unittest

from aisre.baseline import (ChangeRecord, IncidentRecord, compute_baseline)

AS_OF = "2026-07-15T00:00:00Z"


def inc(iid, service="payment-api", cause="RECENT_RELEASE_REGRESSION",
        severity="S2", started="2026-07-01T10:00:00Z",
        mitigated="2026-07-01T10:30:00Z"):
    return IncidentRecord(incident_id=iid, service=service, cause_code=cause,
                          severity=severity, started_at=started,
                          mitigated_at=mitigated)


def chg(cid, service="payment-api", deployed="2026-07-01T09:00:00Z",
        failed=False):
    return ChangeRecord(change_id=cid, service=service, deployed_at=deployed,
                        failed=failed)


class TestWindowFilter(unittest.TestCase):
    def test_incident_older_than_window_excluded(self):
        report = compute_baseline(
            incidents=[
                inc("old-1", started="2026-04-10T10:00:00Z",
                    mitigated="2026-04-10T10:30:00Z"),   # 96 天前，窗口外
                inc("new-1"),
            ],
            changes=[], as_of=AS_OF, window_days=90)
        stats = report.by_service_scenario[("payment-api",
                                            "RECENT_RELEASE_REGRESSION")]
        self.assertEqual(stats.count, 1)

    def test_open_incident_excluded_from_mttm_but_counted(self):
        report = compute_baseline(
            incidents=[inc("a"), inc("open-1", mitigated=None)],
            changes=[], as_of=AS_OF)
        stats = report.by_service_scenario[("payment-api",
                                            "RECENT_RELEASE_REGRESSION")]
        self.assertEqual(stats.count, 1)
        self.assertEqual(report.open_excluded, 1)


class TestMTTMStats(unittest.TestCase):
    def test_median_and_p75_nearest_rank(self):
        # MTTM 依次为 10 / 20 / 30 / 40 分钟
        incidents = [
            inc("a", mitigated="2026-07-01T10:10:00Z"),
            inc("b", mitigated="2026-07-01T10:20:00Z"),
            inc("c", mitigated="2026-07-01T10:30:00Z"),
            inc("d", mitigated="2026-07-01T10:40:00Z"),
        ]
        report = compute_baseline(incidents=incidents, changes=[], as_of=AS_OF)
        stats = report.by_service_scenario[("payment-api",
                                            "RECENT_RELEASE_REGRESSION")]
        self.assertEqual(stats.count, 4)
        self.assertEqual(stats.mttm_median_min, 20.0)   # nearest-rank(0.5)
        self.assertEqual(stats.mttm_p75_min, 30.0)      # nearest-rank(0.75)

    def test_grouping_by_scenario_and_severity(self):
        incidents = [
            inc("a"),
            inc("b", cause="CAPACITY_SATURATION", severity="S1",
                mitigated="2026-07-01T11:00:00Z"),
        ]
        report = compute_baseline(incidents=incidents, changes=[], as_of=AS_OF)
        self.assertIn(("payment-api", "CAPACITY_SATURATION"),
                      report.by_service_scenario)
        self.assertIn(("payment-api", "S1"), report.by_service_severity)
        self.assertIn(("payment-api", "S2"), report.by_service_severity)


class TestChangeFailureRate(unittest.TestCase):
    def test_rate_computed_within_window(self):
        changes = ([chg(f"c{i}") for i in range(8)]
                   + [chg("f1", failed=True), chg("f2", failed=True)]
                   + [chg("old", deployed="2026-01-01T00:00:00Z", failed=True)])
        report = compute_baseline(incidents=[], changes=changes, as_of=AS_OF)
        stats = report.change_stats["payment-api"]
        self.assertEqual(stats.total_changes, 10)   # 窗口外的 old 不算
        self.assertEqual(stats.failed_changes, 2)
        self.assertAlmostEqual(stats.failure_rate, 0.2)

    def test_no_changes_yields_none_rate(self):
        report = compute_baseline(incidents=[], changes=[], as_of=AS_OF)
        self.assertEqual(report.change_stats, {})


class TestReportSerialization(unittest.TestCase):
    def test_empty_input_no_crash(self):
        report = compute_baseline(incidents=[], changes=[], as_of=AS_OF)
        self.assertEqual(report.by_service_scenario, {})
        self.assertEqual(report.open_excluded, 0)

    def test_to_dict_is_json_serializable(self):
        report = compute_baseline(
            incidents=[inc("a")], changes=[chg("c1", failed=True)],
            as_of=AS_OF)
        text = json.dumps(report.to_dict(), ensure_ascii=False)
        parsed = json.loads(text)
        self.assertEqual(parsed["as_of"], AS_OF)
        self.assertEqual(parsed["window_days"], 90)
        self.assertEqual(
            parsed["by_service_scenario"]
            ["payment-api+RECENT_RELEASE_REGRESSION"]["count"], 1)
        self.assertEqual(parsed["change_stats"]["payment-api"]["failure_rate"], 1.0)
