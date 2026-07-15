"""线 2：git 变更窗口 + hunk 解析、审计变更窗口过滤。"""
import json
import tempfile
import unittest
from pathlib import Path

from rca.change_sources import (collect_audit_changes, collect_git_changes,
                                resolve_repo_path)
from rca.schemas import ChangeType
from tests import helpers


class TestGitChanges(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.info = helpers.make_service_repo(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_window_excludes_baseline_commit(self):
        changes = collect_git_changes(
            self.info["repo"], "order-service",
            helpers.NPE_FIRST_SEEN, window_hours=72)
        shas = [c.change_id for c in changes]
        self.assertIn(self.info["bad_sha"], shas)
        self.assertNotIn(self.info["baseline_sha"], shas)

    def test_wide_window_includes_both(self):
        changes = collect_git_changes(
            self.info["repo"], "order-service",
            helpers.NPE_FIRST_SEEN, window_hours=24 * 30)
        self.assertEqual(len(changes), 2)
        # 按时间倒序：最新的在前
        self.assertEqual(changes[0].change_id, self.info["bad_sha"])

    def test_hunk_regions_cover_npe_line(self):
        changes = collect_git_changes(
            self.info["repo"], "order-service",
            helpers.NPE_FIRST_SEEN, window_hours=72)
        bad = changes[0]
        self.assertEqual(bad.change_type, ChangeType.CODE)
        pricing_regions = [r for r in bad.touched
                           if r.file.endswith("PricingService.java")]
        self.assertTrue(pricing_regions)
        npe = self.info["npe_line"]
        self.assertTrue(any(r.line_start <= npe <= r.line_end for r in pricing_regions),
                        f"NPE 行 {npe} 应落在变更区间内: {pricing_regions}")

    def test_resolve_repo_path_by_basename(self):
        path = resolve_repo_path(self.info["repo"], self.info["bad_sha"],
                                 "PricingService.java")
        self.assertEqual(path, f"{helpers.SRC}/service/PricingService.java")
        self.assertIsNone(resolve_repo_path(
            self.info["repo"], self.info["bad_sha"], "NoSuch.java"))


class TestAuditChanges(unittest.TestCase):
    def test_window_and_relation(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = Path(tmp) / "audit.json"
            audit.write_text(json.dumps(helpers.AUDIT_ENTRIES), encoding="utf-8")
            changes = collect_audit_changes(
                str(audit), "order-service", helpers.NPE_FIRST_SEEN, 72)
            # CFG-8802 在错误首现之后，必须被排除——之后的变更不可能是原因
            ids = [c.change_id for c in changes]
            self.assertEqual(ids, ["CFG-8801"])
            self.assertEqual(changes[0].relation, "other-service")
            self.assertEqual(changes[0].change_type, ChangeType.CONFIG)


if __name__ == "__main__":
    unittest.main()
