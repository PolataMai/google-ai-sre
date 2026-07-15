"""上游推导：多模块 pom 依赖 → workspace 仓库匹配；DTD 拒绝。"""
import tempfile
import unittest
from pathlib import Path

from rca.upstream_discovery import (collect_dependencies, index_workspace,
                                    suggest_upstreams)

POM_NS = 'xmlns="http://maven.apache.org/POM/4.0.0"'


def _pom(artifact: str, group: str = "com.example", deps: list = (),
         parent_group: str = "") -> str:
    dep_xml = "".join(
        f"<dependency><groupId>{g}</groupId><artifactId>{a}</artifactId></dependency>"
        for g, a in deps)
    group_xml = (f"<groupId>{group}</groupId>" if group
                 else f"<parent><groupId>{parent_group}</groupId></parent>")
    return (f'<?xml version="1.0"?><project {POM_NS}>'
            f"{group_xml}<artifactId>{artifact}</artifactId>"
            f"<dependencies>{dep_xml}</dependencies></project>")


class TestUpstreamDiscovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        ws = Path(self.tmp.name)
        # 服务仓：父 pom + core 子模块，依赖 coupon-lib（内部）与 guava（外部）
        svc = ws / "order-service"
        (svc / "order-core").mkdir(parents=True)
        (svc / "pom.xml").write_text(_pom("order-parent"), encoding="utf-8")
        (svc / "order-core" / "pom.xml").write_text(
            _pom("order-core", group="", parent_group="com.example", deps=[
                ("com.example", "coupon-lib"),
                ("com.example", "order-parent"),      # 仓内自依赖，应排除
                ("com.google.guava", "guava"),        # 外部依赖，workspace 无匹配
            ]), encoding="utf-8")
        # workspace 里的上游仓
        lib = ws / "coupon-lib"
        lib.mkdir()
        (lib / "pom.xml").write_text(_pom("coupon-lib"), encoding="utf-8")
        # 无关仓
        other = ws / "pay-service"
        other.mkdir()
        (other / "pom.xml").write_text(_pom("pay-service"), encoding="utf-8")
        self.ws, self.svc = str(ws), str(svc)

    def tearDown(self):
        self.tmp.cleanup()

    def test_collect_dependencies_excludes_self(self):
        deps = collect_dependencies(self.svc)
        self.assertIn(("com.example", "coupon-lib"), deps)
        self.assertNotIn(("com.example", "order-parent"), deps)

    def test_group_prefix_filter(self):
        deps = collect_dependencies(self.svc, group_prefix="com.example")
        self.assertEqual(deps, [("com.example", "coupon-lib")])

    def test_index_excludes_service_repo_itself(self):
        idx = index_workspace(self.ws, exclude=self.svc)
        self.assertIn("coupon-lib", idx)
        self.assertNotIn("order-core", idx)

    def test_suggestions(self):
        out = suggest_upstreams(self.svc, self.ws, group_prefix="com.example")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].name, "coupon-lib")
        self.assertEqual(out[0].artifact, "coupon-lib")
        self.assertTrue(Path(out[0].path).samefile(Path(self.ws) / "coupon-lib"))

    def test_doctype_pom_rejected(self):
        evil = Path(self.ws) / "evil-lib"
        evil.mkdir()
        (evil / "pom.xml").write_text(
            '<?xml version="1.0"?><!DOCTYPE project [<!ENTITY x "y">]>'
            "<project><artifactId>evil-lib</artifactId></project>",
            encoding="utf-8")
        idx = index_workspace(self.ws, exclude=self.svc)
        self.assertNotIn("evil-lib", idx)


if __name__ == "__main__":
    unittest.main()
