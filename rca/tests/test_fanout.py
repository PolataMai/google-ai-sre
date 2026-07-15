"""跨仓 fan-out：堆栈指向上游共享库时，上游仓库参与取证/图合并/锚定。

场景：coupon-lib（上游共享库）的坏提交把 fetch 改成直接抛
IllegalStateException；order-service（主仓）窗口内也有自己的坏提交（NPE 那个），
它对本签名只是 GRAPH 级候选。期望：上游 DIRECT 命中胜出 → CONFIRMED 归因上游
提交，且在上游仓完成版本锚定；主仓提交仍留在候选排名里。
"""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from rca import cli
from tests import helpers

COUPON_CLIENT_BASELINE = """package com.example.coupon;

public class CouponClient {

    public String fetch(String couponId) {
        return "coupon:" + couponId;
    }
}
"""

COUPON_CLIENT_BAD = """package com.example.coupon;

public class CouponClient {

    public String fetch(String couponId) {
        throw new IllegalStateException("coupon center circuit open");
    }
}
"""

UPSTREAM_BAD_TIME = "2026-07-11T09:30:00+00:00"


def make_upstream_repo(root: str) -> dict:
    subprocess.run(["git", "init", "-q", "-b", "main", root], check=True)
    subprocess.run(["git", "-C", root, "config", "user.email", "d@e"], check=True)
    subprocess.run(["git", "-C", root, "config", "user.name", "d"], check=True)
    src = Path(root) / "src/main/java/com/example/coupon/CouponClient.java"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(COUPON_CLIENT_BASELINE, encoding="utf-8")
    baseline = helpers.commit_all(root, "feat: coupon client", helpers.BASELINE_TIME)
    src.write_text(COUPON_CLIENT_BAD, encoding="utf-8")
    bad = helpers.commit_all(root, "fix: 熔断打开时快速失败", UPSTREAM_BAD_TIME)
    return {"repo": root, "baseline_sha": baseline, "bad_sha": bad,
            "throw_line": helpers.line_of(COUPON_CLIENT_BAD, "throw new IllegalStateException")}


def make_fanout_log(up: dict, primary: dict) -> str:
    return "\n".join([
        "2026-07-11 14:23:50.000 ERROR [order-service] h : coupon fetch failed",
        "java.lang.IllegalStateException: coupon center circuit open",
        f"\tat com.example.coupon.CouponClient.fetch(CouponClient.java:{up['throw_line']})",
        f"\tat com.example.order.service.OrderService.createOrder(OrderService.java:{primary['os_call_line']})",
        "\tat java.base/java.lang.Thread.run(Thread.java:833)",
        "",
    ])


class TestFanout(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.primary = helpers.make_service_repo(str(root / "order-service"))
        cls.up = make_upstream_repo(str(root / "coupon-lib"))

        (root / "app.log").write_text(
            make_fanout_log(cls.up, cls.primary), encoding="utf-8")
        (root / "alert.json").write_text(json.dumps({
            "incident_id": "INC-20260711-002", "service": "order-service",
            "alert_time": helpers.ALERT_TIME,
            "deployed_commit": cls.primary["bad_sha"],
            "business_packages": ["com.example"],
        }), encoding="utf-8")
        cls.json_out = root / "report.json"
        cls.exit_code = cli.main([
            "run", "--alert", str(root / "alert.json"),
            "--logs", str(root / "app.log"),
            "--repo", cls.primary["repo"],
            "--upstream", f"coupon-lib={cls.up['repo']}@{cls.up['bad_sha']}",
            "--json-out", str(cls.json_out)])
        cls.report = json.loads(cls.json_out.read_text(encoding="utf-8"))
        cls.verdict = cls.report["verdicts"][0]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_confirmed_attributed_to_upstream_commit(self):
        self.assertEqual(self.exit_code, 0)
        self.assertEqual(self.verdict["tier"], "CONFIRMED")
        self.assertEqual(self.verdict["mechanism"], "DIRECT")
        self.assertEqual(self.verdict["change_id"], self.up["bad_sha"])

    def test_anchored_in_upstream_repo(self):
        anchor = next(e for e in self.verdict["evidence_chain"]
                      if e["kind"] == "code_anchor")
        self.assertIn("IllegalStateException", anchor["detail"])
        self.assertIn("CouponClient.java", anchor["detail"])

    def test_upstream_relation_marked(self):
        by_id = {c["change_id"]: c for c in self.report["candidates"]}
        self.assertEqual(by_id[self.up["bad_sha"]]["relation"], "upstream")
        self.assertEqual(by_id[self.up["bad_sha"]]["service"], "coupon-lib")

    def test_primary_commit_stays_in_ranking_as_graph(self):
        """主仓坏提交改的 applyCoupon 在故障可达集内 → GRAPH 候选，供复核。"""
        ranked = {c["change_id"]: c["mechanism"]
                  for c in self.verdict["ranked_candidates"]}
        self.assertEqual(ranked.get(self.primary["bad_sha"]), "GRAPH")

    def test_mitigation_targets_upstream(self):
        self.assertIn(self.up["bad_sha"][:12], self.report["mitigation"][0])


if __name__ == "__main__":
    unittest.main()
