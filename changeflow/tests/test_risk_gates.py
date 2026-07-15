"""风险画像五因子 + 三道门 + 异常关联。"""
import tempfile
import unittest
from pathlib import Path

from changeflow import gates
from changeflow.deps import ServiceGraph
from changeflow.risk import RiskContext, profile_change
from changeflow.schemas import RiskLevel, Source, Status
from changeflow.timeline import Timeline
from tests import helpers


def _ctx(tmp: str, **kw) -> RiskContext:
    graph = ServiceGraph.from_malldill_services(helpers.write_services_json(tmp))
    return RiskContext(graph=graph,
                       core_services=kw.get("core", {"order", "pay", "seckill"}),
                       incident_counts=kw.get("incidents", {}),
                       coverage=kw.get("coverage", {}))


class TestRiskProfile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ctx = _ctx(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_high_risk_scenario(self):
        """周五晚、大范围、命中核心链路、低覆盖、无灰度无回滚 → HIGH。"""
        ev = helpers.make_event(
            source=Source.CODE, service="product",
            timestamp=helpers.FRIDAY_EVENING, gray=False, rollback_plan="",
            details={"files": [f"F{i}.java" for i in range(25)]})
        ctx = _ctx(self.tmp.name, coverage={"product": 40.0},
                   incidents={"product": 2})
        prof = profile_change(ev, ctx)
        self.assertEqual(prof.level, RiskLevel.HIGH)
        names = {f.name for f in prof.factors}
        self.assertEqual(names, {"scope", "blast_radius", "core_link",
                                 "history", "coverage", "timing", "capability"})
        # 爆炸半径含传导后的 points；core_link 由下游 order 命中
        self.assertIn("order", prof.blast_services)
        self.assertIn("points", prof.blast_services)
        # 每个因子必须带证据
        self.assertTrue(all(f.evidence for f in prof.factors))

    def test_low_risk_scenario(self):
        """周二中午、小配置、有灰度回滚、非核心且无下游 → LOW。"""
        ev = helpers.make_event(
            source=Source.CONFIG, service="points",
            timestamp=helpers.TUESDAY_NOON,
            details={"keys": ["log.level"]})
        prof = profile_change(ev, self.ctx)
        self.assertEqual(prof.level, RiskLevel.LOW)
        self.assertEqual(prof.blast_services, [])

    def test_ddl_scope_scores_20(self):
        ev = helpers.make_event(source=Source.DB, service="order",
                                details={"tables": ["t_order"], "ddl": True})
        prof = profile_change(ev, self.ctx)
        scope = next(f for f in prof.factors if f.name == "scope")
        self.assertEqual(scope.points, 20)
        self.assertIn("DDL", scope.evidence)


class TestPrecheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ctx = _ctx(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _report(self, **kw):
        ev = helpers.make_event(**kw)
        return gates.precheck(ev, profile_change(ev, self.ctx), self.ctx), ev

    def test_high_without_rollback_blocked(self):
        report, _ = self._report(
            source=Source.CODE, service="product",
            timestamp=helpers.FRIDAY_EVENING, gray=False, rollback_plan="",
            details={"files": [f"F{i}.java" for i in range(25)]})
        self.assertEqual(report.verdict, "BLOCK")
        blocking = [c.name for c in report.checks if c.blocking]
        self.assertIn("回滚能力", blocking)
        self.assertIn("灰度能力", blocking)

    def test_low_with_capabilities_passes_with_info(self):
        report, _ = self._report(source=Source.CONFIG, service="points",
                                 details={"keys": ["log.level"]})
        self.assertNotEqual(report.verdict, "BLOCK")

    def test_high_risk_dependency_named(self):
        report, _ = self._report(source=Source.CODE, service="product",
                                 details={"files": ["A.java"]})
        dep = next(c for c in report.checks if c.name == "高风险依赖")
        self.assertIn("order", dep.detail)   # 核心服务 order 被点名


class TestWatchAccept(unittest.TestCase):
    T = helpers.TUESDAY_NOON

    def test_step_change_detected(self):
        ev = helpers.make_event(timestamp=self.T)
        metrics = {"error_rate": helpers.metric_series(self.T, base_value=0.5,
                                                       post_value=5.0),
                   "p99_ms": helpers.metric_series(self.T, base_value=30,
                                                   post_value=31, wobble=1.0)}
        report = gates.watch(ev, metrics)
        self.assertEqual(report.verdict, "DRIFTED")
        drifted = {d.metric for d in report.drifts if d.drifted}
        self.assertEqual(drifted, {"error_rate"})   # p99 稳态不误报

    def test_tiny_wobble_not_flagged(self):
        """方差极小的平稳指标：z 大但相对变化小，双条件挡住误报。"""
        ev = helpers.make_event(timestamp=self.T)
        metrics = {"qps": helpers.metric_series(self.T, base_value=1000,
                                                post_value=1002, wobble=0.5)}
        self.assertEqual(gates.watch(ev, metrics).verdict, "STEADY")

    def test_accept_rule_violation_and_missing_data(self):
        ev = helpers.make_event(timestamp=self.T)
        metrics = {"error_rate": helpers.metric_series(self.T, base_value=0.5,
                                                       post_value=5.0)}
        report = gates.accept(ev, metrics,
                              rules={"error_rate": {"max": 1.0},
                                     "success_rate": {"min": 99.0}})
        self.assertEqual(report.verdict, "REJECTED")
        self.assertTrue(any("超上限" in v for v in report.rule_violations))
        self.assertTrue(any("无数据" in v for v in report.rule_violations))

    def test_accept_steady_passes(self):
        ev = helpers.make_event(timestamp=self.T)
        metrics = {"error_rate": helpers.metric_series(self.T, base_value=0.5,
                                                       post_value=0.5)}
        report = gates.accept(ev, metrics, rules={"error_rate": {"max": 1.0}})
        self.assertEqual(report.verdict, "ACCEPTED")


class TestCorrelate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ctx = _ctx(self.tmp.name)
        self.tl = Timeline(str(Path(self.tmp.name) / "tl.jsonl"))
        self.tl.ingest([
            # 异常前 1h：上游 product 的代码变更（真凶形态）
            helpers.make_event("suspect-product", source=Source.CODE,
                               service="product", timestamp="2026-07-14T11:10:00",
                               gray=False, rollback_plan="",
                               details={"files": [f"F{i}.java" for i in range(25)]}),
            # 异常前 20h：无关服务配置变更
            helpers.make_event("noise-marketing", source=Source.CONFIG,
                               service="notification",
                               timestamp="2026-07-13T16:00:00",
                               details={"keys": ["tpl"]}),
            # 异常前 30min 但已回滚：不参与
            helpers.make_event("rolled-back", source=Source.CONFIG,
                               service="order", timestamp="2026-07-14T11:40:00",
                               status=Status.ROLLED_BACK,
                               details={"keys": ["x"]}),
        ])

    def tearDown(self):
        self.tmp.cleanup()

    def test_upstream_recent_change_ranked_first(self):
        suspects = gates.correlate(self.tl, "2026-07-14T12:00:00", "order",
                                   self.ctx)
        self.assertEqual(suspects[0].change_id, "suspect-product")
        self.assertTrue(any("上游依赖" in r for r in suspects[0].reasons))
        ids = [s.change_id for s in suspects]
        self.assertNotIn("rolled-back", ids)     # 已回滚不在场

    def test_export_rca_audit_contract(self):
        entries = gates.export_rca_audit(list(self.tl._events.values()))
        # code 变更不导出；config 变更字段符合 rca audit 契约
        ids = {e["id"] for e in entries}
        self.assertNotIn("suspect-product", ids)
        e = next(x for x in entries if x["id"] == "noise-marketing")
        for key in ("id", "type", "service", "timestamp", "summary", "keys"):
            self.assertIn(key, e)
        self.assertEqual(e["type"], "config")


if __name__ == "__main__":
    unittest.main()
