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
from aisre.admission import PilotMetrics, evaluate_l3_admission
from aisre.baseline import ChangeRecord, IncidentRecord, compute_baseline
from aisre.evaluation import evaluate_replays
from aisre.intake import IntakeService, MalformedPayload, UnknownFormat
from aisre.replay import ReplayCase, replay_case
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
    raw = json.loads(Path(args.file).read_text(encoding="utf-8"))
    try:
        plan = ActionPlan.from_dict(raw)
    except (ValueError, TypeError, KeyError) as exc:
        # 契约层的响亮失败(如非法 success_criteria)也当作违规报告,不崩溃
        _print({"action_id": raw.get("action_id"),
                "violations": [f"计划结构非法: {exc}"]})
        return 1
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


def _cmd_intake(args) -> int:
    payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
    try:
        results = IntakeService().intake(payload, args.format)
    except (UnknownFormat, MalformedPayload) as exc:
        _print({"error": str(exc)})
        return 2
    _print({"incidents": [
        {"incident_id": r.incident_id, "created": r.created,
         "service": r.alert.service, "severity": r.alert.severity,
         "source": r.alert.source, "starts_at": r.alert.starts_at}
        for r in results]})
    return 0


def _cmd_replay(args) -> int:
    cases = [ReplayCase.from_dict(d) for d in _read_jsonl(args.cases)]
    report = evaluate_replays([replay_case(c) for c in cases])
    _print(report.to_dict())
    return 0


def _cmd_admission(args) -> int:
    """L3 准入门禁:读试点指标 JSON，输出授权决定。
    不达标退出码 1——开发完成不能凭空开 L3。"""
    raw = json.loads(Path(args.file).read_text(encoding="utf-8"))
    try:
        metrics = PilotMetrics(**raw)
    except TypeError as exc:
        _print({"error": f"试点指标字段不完整: {exc}"})
        return 2
    decision = evaluate_l3_admission(metrics)
    _print(decision.to_dict())
    return 0 if decision.l3_eligible else 1


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

    p_intake = sub.add_parser("intake", help="接入告警 Webhook,生成统一 incident_id")
    p_intake.add_argument("--file", required=True, help="Webhook 负载 JSON 文件")
    p_intake.add_argument("--format", required=True,
                          help="alertmanager / pagerduty / custom")

    p_replay = sub.add_parser("replay", help="回放历史案例并输出评测报告")
    p_replay.add_argument("--cases", required=True, help="ReplayCase JSONL 文件")

    p_adm = sub.add_parser("admission", help="L3 准入门禁(读试点指标,达标才退出 0)")
    p_adm.add_argument("--file", required=True, help="试点指标 JSON 文件")

    args = parser.parse_args(argv)
    handlers = {
        "scenarios": _cmd_scenarios,
        "baseline": _cmd_baseline,
        "validate-plan": _cmd_validate_plan,
        "validate-enrichment": _cmd_validate_enrichment,
        "intake": _cmd_intake,
        "replay": _cmd_replay,
        "admission": _cmd_admission,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
