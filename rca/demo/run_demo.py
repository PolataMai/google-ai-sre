"""端到端演示：构造一次完整的合成线上故障，跑通 CLI 全流程并校验结论。

用法（在 rca/ 项目根目录）：
    python3 demo/run_demo.py [输出目录]

流程：
1. 构造 order-service 假仓库：baseline 提交（有空券保护，窗口外）
   → bad 提交（删掉保护引入 NPE，即发布版本）；
2. 生成故障日志（3 次 NPE + 1 次与变更无关的支付超时）、配置审计、
   预置同指纹历史案例的知识库；
3. 以子进程方式运行 `python3 -m rca.cli run ...`（验证真实命令行入口）；
4. 校验：NPE → CONFIRMED 归因 bad 提交且止血建议为回滚；
   超时 → HYPOTHESIS 不归因；打印报告。
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from rca.log_forensics import analyze_log  # noqa: E402
from tests import helpers                  # noqa: E402


def main() -> int:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
        tempfile.mkdtemp(prefix="rca-demo-"))
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"▶ 演示目录：{out_dir}\n")

    # 1. 合成故障现场
    info = helpers.make_service_repo(str(out_dir / "order-service"))
    (out_dir / "app.log").write_text(helpers.make_incident_log(info), encoding="utf-8")
    (out_dir / "audit.json").write_text(
        json.dumps(helpers.AUDIT_ENTRIES, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "alert.json").write_text(json.dumps({
        "incident_id": "INC-20260711-001",
        "service": "order-service",
        "alert_time": helpers.ALERT_TIME,
        "deployed_commit": info["bad_sha"],
        "business_packages": ["com.example"],
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    sigs = analyze_log(helpers.make_incident_log(info), "order-service", ["com.example"])
    npe_fp = next(s.fingerprint for s in sigs if "NullPointer" in s.exception_type)
    (out_dir / "kb.json").write_text(json.dumps({npe_fp: [{
        "incident_id": "INC-20260301-007", "date": "2026-03-01T08:00:00",
        "tier": "CONFIRMED", "root_cause": "历史上同位置的空券 NPE，当时回滚修复",
        "change_id": "oldsha", "notes": "历史案例"}]}, ensure_ascii=False), encoding="utf-8")

    print(f"  baseline 提交（窗口外）：{info['baseline_sha'][:12]}")
    print(f"  bad 提交（发布版本）  ：{info['bad_sha'][:12]}  ← 期望被定位的根因\n")

    # 2. 真实命令行入口跑全流程
    cmd = [sys.executable, "-m", "rca.cli", "run",
           "--alert", str(out_dir / "alert.json"),
           "--logs", str(out_dir / "app.log"),
           "--repo", str(out_dir / "order-service"),
           "--audit", str(out_dir / "audit.json"),
           "--kb", str(out_dir / "kb.json"), "--write-back",
           "--out", str(out_dir / "report.md"),
           "--json-out", str(out_dir / "report.json")]
    print("▶ 执行：", " ".join(cmd[2:]), "\n")
    proc = subprocess.run(cmd, cwd=PROJECT_ROOT, capture_output=True, text=True)
    print(proc.stdout)
    if proc.returncode != 0:
        print(proc.stderr, file=sys.stderr)
        print("✗ CLI 运行失败")
        return 1

    # 3. 结论校验
    report = json.loads((out_dir / "report.json").read_text(encoding="utf-8"))
    verdicts = {v["fingerprint"]: v for v in report["verdicts"]}
    sig_by_exc = {s["exception_type"]: s for s in report["signatures"]}
    npe_v = verdicts[sig_by_exc["java.lang.NullPointerException"]["fingerprint"]]
    to_v = verdicts[sig_by_exc["java.net.SocketTimeoutException"]["fingerprint"]]

    checks = [
        ("NPE 判定为 CONFIRMED", npe_v["tier"] == "CONFIRMED"),
        ("NPE 归因到 bad 提交", npe_v["change_id"] == info["bad_sha"]),
        ("证据链 = 堆栈帧→版本锚定→diff",
         [e["kind"] for e in npe_v["evidence_chain"]][:3]
         == ["stack_frame", "code_anchor", "diff_hunk"]),
        ("止血建议第一条是回滚 bad 提交",
         "回滚" in report["mitigation"][0]
         and info["bad_sha"][:12] in report["mitigation"][0]),
        ("超时错误无交集 → HYPOTHESIS", to_v["tier"] == "HYPOTHESIS"),
        ("HYPOTHESIS 未硬凑归因", to_v["change_id"] is None),
        ("知识库命中历史案例", any(k["incident_id"] == "INC-20260301-007"
                                   for k in report["kb_matches"])),
        ("无关服务配置变更未被归因",
         "CFG-8801" not in {v["change_id"] for v in report["verdicts"]}),
    ]
    ok = True
    for name, passed in checks:
        print(f"  {'✓' if passed else '✗'} {name}")
        ok &= passed

    print(f"\n▶ 报告：{out_dir / 'report.md'}")
    print("=" * 60)
    print((out_dir / "report.md").read_text(encoding="utf-8"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
