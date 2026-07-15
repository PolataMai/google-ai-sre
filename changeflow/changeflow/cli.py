"""changeflow CLI：变更时间线 → 风险画像 → 三道门 → 异常关联 → rca 衔接。

用法示例：
  python3 -m changeflow.cli ingest-git --repo <repo> --service order --since 2026-07-10 --timeline tl.jsonl
  python3 -m changeflow.cli ingest-audit --input audit.json --timeline tl.jsonl
  python3 -m changeflow.cli precheck --timeline tl.jsonl --change-id git-abc \\
      --services-json <mall-dill>/knowledge/indexes/services.json \\
      --core-services order,pay,product,seckill --coverage cov.json
  python3 -m changeflow.cli accept --timeline tl.jsonl --change-id git-abc \\
      --metrics metrics.json --rules rules.json
  python3 -m changeflow.cli correlate --timeline tl.jsonl \\
      --at "2026-07-11T14:23:05" --service order --services-json ...
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from . import gates, ingest
from .deps import ServiceGraph
from .risk import RiskContext, profile_change
from .schemas import Source, dumps
from .timeline import Timeline


def _load_ctx(args) -> RiskContext:
    if getattr(args, "services_json", None):
        graph = ServiceGraph.from_malldill_services(args.services_json)
    elif getattr(args, "edges_json", None):
        graph = ServiceGraph.from_edges_json(args.edges_json)
    else:
        graph = ServiceGraph()
    if getattr(args, "calls_json", None):
        # project-map 的进程内/跨模块调用边并入（补 Feign/MQ 抓不到的直调）
        graph.merge(ServiceGraph.from_project_map_calls(args.calls_json))
    core = {s.strip() for s in (getattr(args, "core_services", "") or "").split(",")
            if s.strip()}
    incidents = (RiskContext.load_incidents(args.incidents)
                 if getattr(args, "incidents", None) else {})
    coverage = (RiskContext.load_coverage(args.coverage)
                if getattr(args, "coverage", None) else {})
    return RiskContext(graph=graph, core_services=core,
                       incident_counts=incidents, coverage=coverage)


def _get_event(tl: Timeline, change_id: str):
    ev = tl._events.get(change_id)
    if ev is None:
        print(f"时间线里没有变更 {change_id}", file=sys.stderr)
        sys.exit(2)
    return ev


def _load_metrics(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def cmd_ingest_git(args) -> int:
    tl = Timeline(args.timeline)
    n = tl.ingest(ingest.from_git(args.repo, args.service, args.since,
                                  args.until or "", args.ref))
    print(f"新增 {n} 条 code 变更 → {args.timeline}（共 {len(tl)}）")
    return 0


def cmd_ingest_audit(args) -> int:
    tl = Timeline(args.timeline)
    n = tl.ingest(ingest.from_rca_audit(args.input))
    print(f"新增 {n} 条审计变更 → {args.timeline}（共 {len(tl)}）")
    return 0


def cmd_ingest_json(args) -> int:
    tl = Timeline(args.timeline)
    n = tl.ingest(ingest.from_events_json(args.input))
    print(f"新增 {n} 条变更 → {args.timeline}（共 {len(tl)}）")
    return 0


def cmd_timeline(args) -> int:
    tl = Timeline(args.timeline)
    events = tl.query(since=args.since, until=args.until, service=args.service,
                      source=Source(args.source) if args.source else None)
    for ev in events:
        gray = "灰度" if ev.gray else "全量"
        rb = "有回滚" if ev.rollback_plan else "无回滚"
        print(f"{ev.timestamp}  [{ev.source.value:10}] {ev.service:14} "
              f"{ev.change_id:20} {gray}/{rb}  {ev.summary}")
    print(f"—— {len(events)} 条")
    return 0


def cmd_profile(args) -> int:
    tl, ctx = Timeline(args.timeline), _load_ctx(args)
    prof = profile_change(_get_event(tl, args.change_id), ctx)
    if args.json:
        print(dumps(prof.to_dict()))
    else:
        print(f"{prof.change_id}: {prof.level.value}（{prof.score} 分）")
        for f in prof.factors:
            print(f"  +{f.points:<3} [{f.name}] {f.evidence}")
        if prof.blast_services:
            print(f"  爆炸半径：{', '.join(prof.blast_services)}")
    return 0


def cmd_precheck(args) -> int:
    tl, ctx = Timeline(args.timeline), _load_ctx(args)
    ev = _get_event(tl, args.change_id)
    report = gates.precheck(ev, profile_change(ev, ctx), ctx)
    if args.json:
        print(dumps(asdict(report)))
    else:
        print(f"{report.change_id}: {report.verdict}")
        for c in report.checks:
            mark = "✓" if c.ok else ("⛔" if c.blocking else "⚠️")
            print(f"  {mark} [{c.name}] {c.detail}")
    return 0 if report.verdict != "BLOCK" else 1


def cmd_watch(args) -> int:
    tl = Timeline(args.timeline)
    report = gates.watch(_get_event(tl, args.change_id),
                         _load_metrics(args.metrics),
                         baseline_min=args.baseline_min, post_min=args.post_min)
    if args.json:
        print(dumps(asdict(report)))
    else:
        print(f"{report.change_id}: {report.verdict}")
        for d in report.drifts:
            mark = "⚠️" if d.drifted else "✓"
            print(f"  {mark} {d.metric}: {d.detail}")
    return 0 if report.verdict == "STEADY" else 1


def cmd_accept(args) -> int:
    tl = Timeline(args.timeline)
    rules = _load_metrics(args.rules) if args.rules else {}
    report = gates.accept(_get_event(tl, args.change_id),
                          _load_metrics(args.metrics), rules,
                          post_min=args.post_min, baseline_min=args.baseline_min)
    if args.json:
        print(dumps(asdict(report)))
    else:
        print(f"{report.change_id}: {report.verdict}")
        for v in report.rule_violations:
            print(f"  ⛔ {v}")
        for d in report.drift.drifts:
            mark = "⚠️" if d.drifted else "✓"
            print(f"  {mark} {d.metric}: {d.detail}")
        if report.verdict == "REJECTED":
            print("  → 建议：correlate 关联嫌疑变更；确认后按回滚方案回滚")
    return 0 if report.verdict == "ACCEPTED" else 1


def cmd_correlate(args) -> int:
    tl, ctx = Timeline(args.timeline), _load_ctx(args)
    suspects = gates.correlate(tl, args.at, args.service, ctx,
                               window_hours=args.window_hours, top=args.top)
    if args.json:
        print(dumps([{**asdict(s), "event": s.event.to_dict()} for s in suspects]))
    else:
        if not suspects:
            print("窗口内无候选变更——按无变更故障路径排查（流量/数据/第三方/资源）")
        for i, s in enumerate(suspects, 1):
            print(f"{i}. [{s.score:3}分] {s.change_id}  "
                  f"({s.event.source.value}/{s.event.service}) {s.event.summary}")
            for r in s.reasons:
                print(f"     - {r}")
    return 0


def cmd_export_rca(args) -> int:
    tl = Timeline(args.timeline)
    events = tl.query(since=args.since, until=args.until)
    entries = gates.export_rca_audit(events)
    Path(args.out).write_text(json.dumps(entries, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    print(f"导出 {len(entries)} 条非代码变更 → {args.out}"
          f"（rca run --audit 直接可用）")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="changeflow", description="统一变更数据平台")
    sub = p.add_subparsers(dest="cmd", required=True)

    def ctx_flags(sp):
        sp.add_argument("--services-json", help="mall-dill knowledge services.json")
        sp.add_argument("--edges-json", help="通用依赖边 JSON")
        sp.add_argument("--calls-json",
                        help="project-map calls.json（进程内调用边，与上述图合并）")
        sp.add_argument("--core-services", default="", help="核心链路服务，逗号分隔")
        sp.add_argument("--incidents", help="历史故障记录 JSON")
        sp.add_argument("--coverage", help="覆盖率 JSON {service: pct}")

    sp = sub.add_parser("ingest-git");     sp.set_defaults(fn=cmd_ingest_git)
    sp.add_argument("--repo", required=True); sp.add_argument("--service", required=True)
    sp.add_argument("--since", required=True); sp.add_argument("--until")
    sp.add_argument("--ref", default="HEAD"); sp.add_argument("--timeline", required=True)

    sp = sub.add_parser("ingest-audit");   sp.set_defaults(fn=cmd_ingest_audit)
    sp.add_argument("--input", required=True); sp.add_argument("--timeline", required=True)

    sp = sub.add_parser("ingest-json");    sp.set_defaults(fn=cmd_ingest_json)
    sp.add_argument("--input", required=True); sp.add_argument("--timeline", required=True)

    sp = sub.add_parser("timeline");       sp.set_defaults(fn=cmd_timeline)
    sp.add_argument("--timeline", required=True)
    sp.add_argument("--since"); sp.add_argument("--until")
    sp.add_argument("--service", default=""); sp.add_argument("--source")

    sp = sub.add_parser("profile");        sp.set_defaults(fn=cmd_profile)
    sp.add_argument("--timeline", required=True); sp.add_argument("--change-id", required=True)
    sp.add_argument("--json", action="store_true"); ctx_flags(sp)

    sp = sub.add_parser("precheck");       sp.set_defaults(fn=cmd_precheck)
    sp.add_argument("--timeline", required=True); sp.add_argument("--change-id", required=True)
    sp.add_argument("--json", action="store_true"); ctx_flags(sp)

    sp = sub.add_parser("watch");          sp.set_defaults(fn=cmd_watch)
    sp.add_argument("--timeline", required=True); sp.add_argument("--change-id", required=True)
    sp.add_argument("--metrics", required=True)
    sp.add_argument("--baseline-min", type=int, default=30)
    sp.add_argument("--post-min", type=int, default=30)
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("accept");         sp.set_defaults(fn=cmd_accept)
    sp.add_argument("--timeline", required=True); sp.add_argument("--change-id", required=True)
    sp.add_argument("--metrics", required=True); sp.add_argument("--rules")
    sp.add_argument("--baseline-min", type=int, default=30)
    sp.add_argument("--post-min", type=int, default=30)
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("correlate");      sp.set_defaults(fn=cmd_correlate)
    sp.add_argument("--timeline", required=True)
    sp.add_argument("--at", required=True, help="异常时刻 ISO")
    sp.add_argument("--service", required=True, help="异常服务")
    sp.add_argument("--window-hours", type=int, default=24)
    sp.add_argument("--top", type=int, default=5)
    sp.add_argument("--json", action="store_true"); ctx_flags(sp)

    sp = sub.add_parser("export-rca-audit"); sp.set_defaults(fn=cmd_export_rca)
    sp.add_argument("--timeline", required=True); sp.add_argument("--out", required=True)
    sp.add_argument("--since"); sp.add_argument("--until")

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
