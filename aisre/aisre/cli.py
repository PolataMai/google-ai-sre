"""CLI 入口：python3 -m aisre.cli <子命令>

子命令：
- scenarios                                   列出三类试点场景定义
- baseline --incidents f.jsonl [--changes c.jsonl] --as-of TS [--window 90]
- validate-plan --file plan.json --now TS [--scenario CAUSE_CODE]
- validate-enrichment --file enr.json

输出一律为 JSON;校验类命令发现违规时退出码 1。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from aisre.actions import ActionPlan, validate_action_plan
from aisre.baseline import ChangeRecord, IncidentRecord, compute_baseline
from aisre.scenarios import get_scenario, list_scenarios
from aisre.schemas import Enrichment, evidence_coverage, validate_enrichment


def _print(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _read_jsonl(path: str) -> list[dict]:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _cmd_scenarios(_args) -> int:
    _print({"scenarios": [
        {"cause_code": s.cause_code.value,
         "title": s.title,
         "detection_signals": list(s.detection_signals),
         "verification_steps": list(s.verification_steps),
         "allowed_actions": list(s.allowed_actions)}
        for s in list_scenarios()]})
    return 0


def _cmd_baseline(args) -> int:
    incidents = [IncidentRecord(**d) for d in _read_jsonl(args.incidents)]
    changes = ([ChangeRecord(**d) for d in _read_jsonl(args.changes)]
               if args.changes else [])
    report = compute_baseline(incidents=incidents, changes=changes,
                              as_of=args.as_of, window_days=args.window)
    _print(report.to_dict())
    return 0


def _cmd_validate_plan(args) -> int:
    plan = ActionPlan.from_dict(
        json.loads(Path(args.file).read_text(encoding="utf-8")))
    scenario = get_scenario(args.scenario) if args.scenario else None
    violations = validate_action_plan(plan, now=args.now, scenario=scenario)
    _print({"action_id": plan.action_id,
            "plan_hash": plan.plan_hash(),
            "violations": violations})
    return 1 if violations else 0


def _cmd_validate_enrichment(args) -> int:
    enr = Enrichment.from_dict(
        json.loads(Path(args.file).read_text(encoding="utf-8")))
    violations = validate_enrichment(enr)
    _print({"incident_id": enr.incident_id,
            "evidence_coverage": evidence_coverage(enr),
            "violations": violations})
    return 1 if violations else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="aisre")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("scenarios", help="列出试点场景定义")

    p_base = sub.add_parser("baseline", help="计算 90 天基线")
    p_base.add_argument("--incidents", required=True, help="事故记录 JSONL")
    p_base.add_argument("--changes", help="变更记录 JSONL")
    p_base.add_argument("--as-of", dest="as_of", required=True,
                        help="基线截止时刻（ISO 时间）")
    p_base.add_argument("--window", type=int, default=90, help="窗口天数")

    p_plan = sub.add_parser("validate-plan", help="校验 ActionPlan")
    p_plan.add_argument("--file", required=True, help="ActionPlan JSON 文件")
    p_plan.add_argument("--now", required=True, help="当前时刻（ISO，供 TTL 判定）")
    p_plan.add_argument("--scenario", help="所属场景 cause_code（校验动作白名单）")

    p_enr = sub.add_parser("validate-enrichment", help="校验告警丰富结果")
    p_enr.add_argument("--file", required=True, help="Enrichment JSON 文件")

    args = parser.parse_args(argv)
    handlers = {
        "scenarios": _cmd_scenarios,
        "baseline": _cmd_baseline,
        "validate-plan": _cmd_validate_plan,
        "validate-enrichment": _cmd_validate_enrichment,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
