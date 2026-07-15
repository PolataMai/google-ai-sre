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
