"""端到端演示：一次高危发布的完整生命周期，走真实 CLI。

场景（对应平台的三道门）：
  T-1d  notification 配置变更（噪声）
  T     product 服务大范围代码发布（22 文件，未声明灰度/回滚）→ precheck BLOCK
        → 补灰度+回滚声明 → 放行（WARN，核心链路依赖被点名）
  T+n   order 服务错误率阶跃 → watch DRIFTED → accept REJECTED
        → correlate：上游 product 的发布排第一 → 导出 rca audit 深度定位

依赖图优先用 mall-dill 真实 knowledge/indexes/services.json（27 服务），
不存在则退回内置夹具。用法：python3 demo/run_demo.py [输出目录]
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests import helpers  # noqa: E402

MALL_DILL_SERVICES = Path.home() / "Documents/work/mall-dill/knowledge/indexes/services.json"
MALL_DILL_CALLS = Path.home() / "Documents/work/mall-dill/.claude/project-map/calls.json"
CHANGE_TS = "2026-07-14T11:10:00"
ANOMALY_TS = "2026-07-14T11:40:00"


def run(argv, expect=0):
    proc = subprocess.run([sys.executable, "-m", "changeflow.cli", *argv],
                          cwd=ROOT, capture_output=True, text=True)
    print(proc.stdout.rstrip())
    if proc.returncode != expect:
        print(proc.stderr, file=sys.stderr)
        raise SystemExit(f"✗ 期望退出码 {expect}，实际 {proc.returncode}: {argv[0]}")
    return proc.stdout


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
        tempfile.mkdtemp(prefix="changeflow-demo-"))
    out.mkdir(parents=True, exist_ok=True)
    tl = str(out / "timeline.jsonl")

    if MALL_DILL_SERVICES.exists():
        services = str(MALL_DILL_SERVICES)
        print(f"▶ 依赖图：mall-dill 真实索引（{services}）")
    else:
        services = helpers.write_services_json(str(out))
        print("▶ 依赖图：内置夹具（未找到 mall-dill 索引）")
    ctx = ["--services-json", services, "--core-services", "order,pay,seckill,product"]
    if MALL_DILL_CALLS.exists():
        ctx += ["--calls-json", str(MALL_DILL_CALLS)]
        print(f"▶ 进程内调用边：project-map calls.json 已并入（{MALL_DILL_CALLS}）")

    # ---- 1. 统一时间线：多源汇入 ----
    bad = helpers.make_event(
        "rel-2024", source=helpers.Source.CODE, service="product",
        timestamp=CHANGE_TS, summary="商品缓存重构上线", author="dev-a",
        gray=False, rollback_plan="",
        details={"files": [f"cache/F{i}.java" for i in range(22)]})
    noise = helpers.make_event(
        "cfg-9", source=helpers.Source.CONFIG, service="notification",
        timestamp="2026-07-13T15:00:00", summary="通知模板调整", author="ops-b",
        details={"keys": ["tpl.id"]})
    (out / "events.json").write_text(json.dumps(
        [bad.to_dict(), noise.to_dict()], ensure_ascii=False), encoding="utf-8")
    (out / "coverage.json").write_text(json.dumps({"product": 45}), encoding="utf-8")

    print("\n━━ 1. 变更汇入统一时间线 ━━")
    run(["ingest-json", "--input", str(out / "events.json"), "--timeline", tl])
    run(["timeline", "--timeline", tl])

    # ---- 2. 事前：风险画像 + 准入 ----
    print("\n━━ 2. 变更前：风险画像 ━━")
    run(["profile", "--timeline", tl, "--change-id", "rel-2024",
         "--coverage", str(out / "coverage.json"), *ctx])
    print("\n━━ 2b. 变更前：准入检查（应 BLOCK）━━")
    run(["precheck", "--timeline", tl, "--change-id", "rel-2024",
         "--coverage", str(out / "coverage.json"), *ctx], expect=1)

    print("\n━━ 2c. 补灰度+回滚声明后重检（应放行）━━")
    fixed = bad.to_dict()
    fixed["gray"], fixed["rollback_plan"] = True, "回滚镜像 product:v2023"
    (out / "fixed.json").write_text(json.dumps([fixed], ensure_ascii=False),
                                    encoding="utf-8")
    run(["ingest-json", "--input", str(out / "fixed.json"), "--timeline", tl])
    run(["precheck", "--timeline", tl, "--change-id", "rel-2024",
         "--coverage", str(out / "coverage.json"), *ctx])

    # ---- 3. 事中/事后：观测与验收 ----
    (out / "metrics.json").write_text(json.dumps({
        "order_error_rate": helpers.metric_series(CHANGE_TS, base_value=0.3,
                                                  post_value=4.0),
        "order_p99_ms": helpers.metric_series(CHANGE_TS, base_value=40,
                                              post_value=42, wobble=2.0),
    }), encoding="utf-8")
    (out / "rules.json").write_text(
        json.dumps({"order_error_rate": {"max": 1.0}}), encoding="utf-8")

    print("\n━━ 3. 变更中：指标偏移观测（应 DRIFTED）━━")
    run(["watch", "--timeline", tl, "--change-id", "rel-2024",
         "--metrics", str(out / "metrics.json")], expect=1)
    print("\n━━ 3b. 变更后：自动验收（应 REJECTED）━━")
    run(["accept", "--timeline", tl, "--change-id", "rel-2024",
         "--metrics", str(out / "metrics.json"),
         "--rules", str(out / "rules.json")], expect=1)

    # ---- 4. 异常关联 + rca 衔接 ----
    print("\n━━ 4. 异常 → 变更关联（真凶应排第一）━━")
    text = run(["correlate", "--timeline", tl, "--at", ANOMALY_TS,
                "--service", "order", *ctx])
    assert "rel-2024" in text.splitlines()[0], "真凶未排第一"
    if MALL_DILL_CALLS.exists():
        # 进程内直调边并入后，order→product 的上游关系必须被点名
        assert "上游依赖" in text, "calls.json 已并入但未识别出上游关系"

    run(["export-rca-audit", "--timeline", tl, "--out", str(out / "rca-audit.json")])
    entries = json.loads((out / "rca-audit.json").read_text(encoding="utf-8"))
    assert entries and entries[0]["id"] == "cfg-9"

    print(f"\n✓ 全生命周期演示通过（产物在 {out}）")
    print("  下一步衔接：rca run --audit rca-audit.json --repo <product仓> "
          "可对 rel-2024 做证据链级根因定位")
    return 0


if __name__ == "__main__":
    sys.exit(main())
