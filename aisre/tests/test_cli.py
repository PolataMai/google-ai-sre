"""CLI 端到端：scenarios / baseline / validate-plan / validate-enrichment 四个子命令。

约定：输出 JSON 到 stdout；校验类命令发现违规时退出码 1 并列出违规。
"""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from aisre import cli
from tests.test_actions import make_scale_out

AS_OF = "2026-07-15T00:00:00Z"


def run_cli(*argv) -> tuple[int, dict]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli.main(list(argv))
    return code, json.loads(buf.getvalue())


class TestScenariosCommand(unittest.TestCase):
    def test_lists_three_scenarios(self):
        code, out = run_cli("scenarios")
        self.assertEqual(code, 0)
        self.assertEqual(len(out["scenarios"]), 3)
        self.assertIn("allowed_actions", out["scenarios"][0])


class TestBaselineCommand(unittest.TestCase):
    def test_baseline_from_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            inc_file = Path(tmp) / "incidents.jsonl"
            inc_file.write_text("\n".join([
                json.dumps({"incident_id": "a", "service": "payment-api",
                            "cause_code": "RECENT_RELEASE_REGRESSION",
                            "severity": "S2",
                            "started_at": "2026-07-01T10:00:00Z",
                            "mitigated_at": "2026-07-01T10:30:00Z"}),
                json.dumps({"incident_id": "b", "service": "payment-api",
                            "cause_code": "CAPACITY_SATURATION",
                            "severity": "S1",
                            "started_at": "2026-07-02T10:00:00Z",
                            "mitigated_at": None}),
            ]), encoding="utf-8")
            chg_file = Path(tmp) / "changes.jsonl"
            chg_file.write_text(json.dumps(
                {"change_id": "c1", "service": "payment-api",
                 "deployed_at": "2026-07-01T09:00:00Z", "failed": True}),
                encoding="utf-8")

            code, out = run_cli("baseline", "--incidents", str(inc_file),
                                "--changes", str(chg_file), "--as-of", AS_OF)
        self.assertEqual(code, 0)
        key = "payment-api+RECENT_RELEASE_REGRESSION"
        self.assertEqual(out["by_service_scenario"][key]["count"], 1)
        self.assertEqual(out["by_service_scenario"][key]["mttm_median_min"], 30.0)
        self.assertEqual(out["open_excluded"], 1)
        self.assertEqual(out["change_stats"]["payment-api"]["failure_rate"], 1.0)


class TestValidatePlanCommand(unittest.TestCase):
    def _write_plan(self, tmp, plan) -> str:
        path = Path(tmp) / "plan.json"
        path.write_text(json.dumps(plan.to_dict(), ensure_ascii=False),
                        encoding="utf-8")
        return str(path)

    def test_valid_plan_exit_zero_and_prints_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_plan(tmp, make_scale_out())
            code, out = run_cli("validate-plan", "--file", path,
                                "--now", "2026-07-15T10:12:00Z",
                                "--scenario", "CAPACITY_SATURATION")
        self.assertEqual(code, 0)
        self.assertEqual(out["violations"], [])
        self.assertEqual(len(out["plan_hash"]), 64)

    def test_invalid_plan_exit_one(self):
        bad = make_scale_out(parameters={"original_replicas": 20,
                                         "target_replicas": 40})
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_plan(tmp, bad)
            code, out = run_cli("validate-plan", "--file", path,
                                "--now", "2026-07-15T10:12:00Z")
        self.assertEqual(code, 1)
        self.assertNotEqual(out["violations"], [])

    def test_malformed_success_criteria_reported_not_crash(self):
        # 契约层响亮失败:非法 success_criteria 报告为违规、退出 1,不崩溃
        plan_dict = make_scale_out().to_dict()
        plan_dict["success_criteria"] = [{"metric": "error_rate", "op": "<",
                                          "threshold": None}]  # 数值缺阈值
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plan.json"
            path.write_text(json.dumps(plan_dict, ensure_ascii=False),
                            encoding="utf-8")
            code, out = run_cli("validate-plan", "--file", str(path),
                                "--now", "2026-07-15T10:12:00Z")
        self.assertEqual(code, 1)
        self.assertTrue(any("结构非法" in v for v in out["violations"]))


class TestIntakeCommand(unittest.TestCase):
    def test_intake_alertmanager_webhook(self):
        payload = {"alerts": [{
            "fingerprint": "abc123",
            "labels": {"alertname": "HighErrorRate",
                       "service": "payment-api", "severity": "critical"},
            "startsAt": "2026-07-15T10:08:00Z",
        }]}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "webhook.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            code, out = run_cli("intake", "--file", str(path),
                                "--format", "alertmanager")
        self.assertEqual(code, 0)
        inc = out["incidents"][0]
        self.assertTrue(inc["incident_id"].startswith("inc-"))
        self.assertTrue(inc["created"])
        self.assertEqual(inc["service"], "payment-api")

    def test_intake_unknown_format_exit_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "webhook.json"
            path.write_text("{}", encoding="utf-8")
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = cli.main(["intake", "--file", str(path),
                                 "--format", "zabbix"])
        self.assertEqual(code, 2)


class TestReplayCommand(unittest.TestCase):
    def test_replay_cases_and_report(self):
        from tests.test_replay import CASE
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cases.jsonl"
            path.write_text(json.dumps(CASE, ensure_ascii=False) + "\n",
                            encoding="utf-8")
            code, out = run_cli("replay", "--cases", str(path))
        self.assertEqual(code, 0)
        self.assertEqual(out["total_cases"], 1)
        self.assertEqual(out["top3_recall"], 1.0)
        self.assertEqual(out["exact_match_rate"], 1.0)


class TestAdmissionCommand(unittest.TestCase):
    def _run(self, tmp, metrics):
        path = Path(tmp) / "pilot.json"
        path.write_text(json.dumps(metrics, ensure_ascii=False),
                        encoding="utf-8")
        return run_cli("admission", "--file", str(path))

    def _full_pass(self):
        return dict(
            pilot_weeks=8.0, valid_incidents=30, shadow_cases=520,
            real_l2_executions=55, exact_match_total=500,
            exact_match_hits=499, weeks_continuous_compliant=8,
            ai_change_failure_rate=0.03, baseline_change_failure_rate=0.05,
            policy_bypasses=0, severe_wrong_actions=0,
            ai_caused_severe_incidents=0, fault_injection_pass_rate=1.0,
            dual_approved=True)

    def test_full_pass_exit_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out = self._run(tmp, self._full_pass())
        self.assertEqual(code, 0)
        self.assertTrue(out["l3_eligible"])

    def test_development_complete_exit_one(self):
        # 开发完成、回放刷满 Shadow,但没试点数据 → 退出 1,列出缺口
        m = self._full_pass()
        m.update(pilot_weeks=0.0, valid_incidents=0, real_l2_executions=0,
                 weeks_continuous_compliant=0, ai_change_failure_rate=None,
                 dual_approved=False)
        with tempfile.TemporaryDirectory() as tmp:
            code, out = self._run(tmp, m)
        self.assertEqual(code, 1)
        self.assertFalse(out["l3_eligible"])
        self.assertIn("pilot_duration", out["blocking"])
        self.assertIn("dual_approval", out["blocking"])


class TestValidateEnrichmentCommand(unittest.TestCase):
    def test_enrichment_with_uncovered_fact_exit_one(self):
        enr = {"incident_id": "inc-x",
               "alert_received_at": "2026-07-15T10:00:00Z",
               "evidences": [],
               "facts": [{"fact_id": "f1", "text": "无证据",
                          "observed_at": "2026-07-15T10:01:00Z",
                          "evidence_ids": []}],
               "hypotheses": []}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "enr.json"
            path.write_text(json.dumps(enr, ensure_ascii=False), encoding="utf-8")
            code, out = run_cli("validate-enrichment", "--file", str(path))
        self.assertEqual(code, 1)
        self.assertEqual(out["evidence_coverage"], 0.0)
        self.assertNotEqual(out["violations"], [])
