"""ingestor（git/rca-audit/通用 JSON）+ CLI 全生命周期端到端。"""
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from changeflow import cli, ingest
from changeflow.schemas import Source
from changeflow.timeline import Timeline
from tests import helpers

RCA_AUDIT = [
    {"id": "apollo-1", "type": "config", "service": "order-service",
     "timestamp": "2026-07-14T09:00:00+00:00", "summary": "调低超时",
     "author": "ops", "keys": ["timeout.ms"]},
    {"id": "ddl-1", "type": "db", "service": "order",
     "timestamp": "2026-07-14T08:00:00+00:00", "summary": "加索引",
     "author": "dba", "keys": ["t_order"], "relation": "shared-db"},
]


def _make_repo(root: str) -> str:
    subprocess.run(["git", "init", "-q", "-b", "main", root], check=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        subprocess.run(["git", "-C", root, "config", k, v], check=True)
    Path(root, "A.java").write_text("class A {}", encoding="utf-8")
    Path(root, "B.java").write_text("class B {}", encoding="utf-8")
    env = dict(os.environ, GIT_AUTHOR_DATE="2026-07-14T10:00:00+00:00",
               GIT_COMMITTER_DATE="2026-07-14T10:00:00+00:00")
    subprocess.run(["git", "-C", root, "add", "-A"], check=True)
    subprocess.run(["git", "-C", root, "commit", "-q", "-m", "feat: 初始"],
                   check=True, env=env)
    return subprocess.run(["git", "-C", root, "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()


class TestIngest(unittest.TestCase):
    def test_git_ingest_with_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            sha = _make_repo(tmp)
            events = ingest.from_git(tmp, "order", "2026-07-13")
            self.assertEqual(len(events), 1)
            ev = events[0]
            self.assertEqual(ev.change_id, f"git-{sha[:12]}")
            self.assertEqual(ev.source, Source.CODE)
            self.assertEqual(sorted(ev.details["files"]), ["A.java", "B.java"])

    def test_rca_audit_bridge(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "audit.json"
            p.write_text(json.dumps(RCA_AUDIT), encoding="utf-8")
            events = ingest.from_rca_audit(str(p))
            cfg = next(e for e in events if e.change_id == "apollo-1")
            ddl = next(e for e in events if e.change_id == "ddl-1")
            self.assertEqual(cfg.source, Source.CONFIG)
            self.assertEqual(cfg.scope_items(), ["timeout.ms"])
            self.assertEqual(ddl.source, Source.DB)
            self.assertTrue(ddl.is_ddl())
            self.assertEqual(ddl.details["relation"], "shared-db")


class TestCliEndToEnd(unittest.TestCase):
    """CLI 全生命周期：ingest → profile → precheck(BLOCK→修复→通过) →
    watch/accept(REJECTED) → correlate 命中真凶 → export 给 rca。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.tl_path = str(root / "tl.jsonl")
        cls.services = helpers.write_services_json(cls.tmp.name)

        # 变更事件：product 高危发布（真凶）+ 无关配置
        bad = helpers.make_event(
            "rel-2024", source=Source.CODE, service="product",
            timestamp="2026-07-14T11:10:00", summary="商品缓存重构上线",
            gray=False, rollback_plan="",
            details={"files": [f"cache/F{i}.java" for i in range(22)]})
        noise = helpers.make_event(
            "cfg-9", source=Source.CONFIG, service="notification",
            timestamp="2026-07-13T15:00:00", summary="通知模板调整",
            details={"keys": ["tpl.id"]})
        (root / "events.json").write_text(
            json.dumps([bad.to_dict(), noise.to_dict()], ensure_ascii=False),
            encoding="utf-8")

        # 指标：变更后 order 服务错误率阶跃
        (root / "metrics.json").write_text(json.dumps({
            "order_error_rate": helpers.metric_series(
                "2026-07-14T11:10:00", base_value=0.3, post_value=4.0)}),
            encoding="utf-8")
        (root / "rules.json").write_text(
            json.dumps({"order_error_rate": {"max": 1.0}}), encoding="utf-8")
        cls.root = root

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _run(self, *argv) -> tuple[int, str]:
        buf = StringIO()
        with redirect_stdout(buf):
            code = cli.main(list(argv))
        return code, buf.getvalue()

    def test_full_lifecycle(self):
        ctx = ["--services-json", self.services,
               "--core-services", "order,pay,seckill"]
        # 1. ingest
        code, _ = self._run("ingest-json", "--input", str(self.root / "events.json"),
                            "--timeline", self.tl_path)
        self.assertEqual(code, 0)

        # 2. 事前：无灰度无回滚的高危发布被 BLOCK（退出码 1）
        code, out = self._run("precheck", "--timeline", self.tl_path,
                              "--change-id", "rel-2024", *ctx)
        self.assertEqual(code, 1)
        self.assertIn("BLOCK", out)

        # 3. 补上灰度与回滚声明后放行（构造新对象再 upsert——
        #    原地改内存对象会让 upsert 误判"无变化"而不落盘）
        tl = Timeline(self.tl_path)
        from changeflow.schemas import ChangeEvent
        d = tl._events["rel-2024"].to_dict()
        d["gray"], d["rollback_plan"] = True, "回滚镜像 v2023"
        tl.upsert(ChangeEvent.from_dict(d))
        code, out = self._run("precheck", "--timeline", self.tl_path,
                              "--change-id", "rel-2024", *ctx)
        self.assertEqual(code, 0)
        self.assertNotIn("BLOCK", out.splitlines()[0])

        # 4. 事中/事后：错误率阶跃 → watch DRIFTED、accept REJECTED
        code, out = self._run("watch", "--timeline", self.tl_path,
                              "--change-id", "rel-2024",
                              "--metrics", str(self.root / "metrics.json"))
        self.assertEqual(code, 1)
        self.assertIn("DRIFTED", out)
        code, out = self._run("accept", "--timeline", self.tl_path,
                              "--change-id", "rel-2024",
                              "--metrics", str(self.root / "metrics.json"),
                              "--rules", str(self.root / "rules.json"))
        self.assertEqual(code, 1)
        self.assertIn("REJECTED", out)

        # 5. 关联：order 异常 → 上游 product 的发布排第一
        code, out = self._run("correlate", "--timeline", self.tl_path,
                              "--at", "2026-07-14T11:40:00",
                              "--service", "order", *ctx)
        self.assertEqual(code, 0)
        first = out.splitlines()[0]
        self.assertIn("rel-2024", first)

        # 6. 导出 rca audit：只含非代码变更且契约字段完整
        out_path = str(self.root / "rca-audit.json")
        code, _ = self._run("export-rca-audit", "--timeline", self.tl_path,
                            "--out", out_path)
        self.assertEqual(code, 0)
        entries = json.loads(Path(out_path).read_text(encoding="utf-8"))
        self.assertEqual([e["id"] for e in entries], ["cfg-9"])
        self.assertEqual(entries[0]["type"], "config")


if __name__ == "__main__":
    unittest.main()
