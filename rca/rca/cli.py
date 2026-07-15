"""CLI 编排：并行两条采证线（进程内顺序执行、逻辑上互不依赖）→ 汇合裁决 → 报告。

用法：
  python3 -m rca.cli run --alert alert.json --logs app.log \\
      --repo /path/to/service-repo [--audit audit.json] \\
      [--code-graph graph.json] [--kb kb.json] [--write-back] \\
      --out report.md --json-out report.json
  python3 -m rca.cli build-graph --java-src /path/to/src --out graph.json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path

from . import (adjudicator, audit_adapters, change_sources, code_graph,
               log_forensics, log_sources, project_map_adapter,
               upstream_discovery)
from .knowledge_base import KnowledgeBase
from .report import render_markdown
from .schemas import CandidateChange, RcaReport, Tier, parse_ts


def window_filter(candidates: list[CandidateChange], anchor_time: str,
                  window_hours: int) -> list[CandidateChange]:
    """按单个签名的 first_seen 精确开窗：[first_seen - window, first_seen]。"""
    anchor = parse_ts(anchor_time)
    since = anchor - timedelta(hours=window_hours)
    return [c for c in candidates if since <= parse_ts(c.timestamp) <= anchor]


def _gather_logs(args: argparse.Namespace, alert: dict,
                 warnings: list[str]) -> str:
    """按参数聚合三种日志源（文件 / 导出命令 / Elasticsearch）。"""
    parts: list[str] = []
    if args.logs:
        text, w = log_sources.read_files(args.logs)
        parts.append(text)
        warnings.extend(w)
    if args.log_cmd:
        text, w = log_sources.read_command(args.log_cmd)
        parts.append(text)
        warnings.extend(w)
    if args.es_url:
        anchor = parse_ts(alert["alert_time"])
        cfg = log_sources.EsConfig(
            url=args.es_url, index=args.es_index,
            time_from=(anchor - timedelta(minutes=args.es_lookback_min)).isoformat(),
            time_to=anchor.isoformat(),
            service=alert.get("service", ""), service_field=args.es_service_field,
            query=args.es_query, timestamp_field=args.es_timestamp_field,
            level_field=args.es_level_field, message_field=args.es_message_field,
            stack_field=args.es_stack_field, size=args.es_size, auth=args.es_auth)
        text, w = log_sources.fetch_elasticsearch(cfg)
        parts.append(text)
        warnings.extend(w)
    return "\n".join(parts)


def cmd_run(args: argparse.Namespace) -> int:
    alert = json.loads(Path(args.alert).read_text(encoding="utf-8"))
    service = alert["service"]
    business_packages = alert.get("business_packages", [])
    deployed_commit = alert.get("deployed_commit", "")

    if not (args.logs or args.log_cmd or args.es_url):
        print("至少提供一种日志源：--logs / --log-cmd / --es-url", file=sys.stderr)
        return 2

    # ---- 上游仓库（跨仓 fan-out）：name=path[@commit] ----
    upstreams: list[tuple[str, str, str]] = []
    for spec in args.upstream:
        name, _, rest = spec.partition("=")
        path, _, commit = rest.partition("@")
        if not name or not path:
            print(f"--upstream 格式应为 name=path[@commit]：{spec}", file=sys.stderr)
            return 2
        upstreams.append((name, path, commit))

    # ---- 线 1：日志取证 ----
    warnings: list[str] = []
    log_text = _gather_logs(args, alert, warnings)
    signatures = log_forensics.analyze_log(
        log_text, service, business_packages, stream_re=args.stream_prefix_re)
    if not signatures:
        print("日志中未发现带堆栈的 ERROR 事件；无法进入裁决。", file=sys.stderr)
        return 2

    # 变更收集锚点取最晚的 first_seen，保证覆盖所有签名的窗口
    latest_anchor = max(s.first_seen for s in signatures)

    # ---- 线 2：变更取证（与线 1 无依赖，可并行；此处顺序执行）----
    candidates: list[CandidateChange] = []
    if args.repo:
        candidates += change_sources.collect_git_changes(
            args.repo, service, latest_anchor, args.window_hours,
            ref=deployed_commit or "HEAD")
    for name, path, commit in upstreams:
        candidates += change_sources.collect_git_changes(
            path, name, latest_anchor, args.window_hours,
            ref=commit or "HEAD", relation="upstream")
    if args.audit:
        candidates += change_sources.collect_audit_changes(
            args.audit, service, latest_anchor, args.window_hours)

    # ---- code graph（跨仓合并：主仓 + 上游仓）----
    if args.code_graph:
        graph = code_graph.CodeGraph.from_json(
            Path(args.code_graph).read_text(encoding="utf-8"))
    else:
        graph = (code_graph.build_from_java_sources(args.repo)
                 if args.repo else code_graph.CodeGraph())
        for _name, path, _commit in upstreams:
            project_map_adapter.merge(
                graph, code_graph.build_from_java_sources(path))

    # ---- 线 3：汇合裁决 ----
    extra_repos = [(path, commit or "HEAD") for _n, path, commit in upstreams]
    verdicts = []
    for sig in signatures:
        in_window = window_filter(candidates, sig.first_seen, args.window_hours)
        verdicts.append(adjudicator.adjudicate(
            sig, in_window, graph, repo=args.repo,
            deployed_commit=deployed_commit, warnings=warnings,
            extra_repos=extra_repos))

    report = RcaReport(
        incident_id=alert.get("incident_id", "INC-UNKNOWN"),
        service=service,
        alert_time=alert.get("alert_time", latest_anchor),
        deployed_commit=deployed_commit,
        signatures=signatures, candidates=candidates, verdicts=verdicts,
        mitigation=adjudicator.build_mitigation(verdicts, candidates),
        warnings=warnings)

    # ---- 知识库：查历史 + 可选回写 ----
    if args.kb:
        kb = KnowledgeBase(args.kb)
        for sig in signatures:
            report.kb_matches += kb.lookup(sig.fingerprint)
        if args.write_back:
            n = kb.write_back(report)
            print(f"知识库回写 {n} 条", file=sys.stderr)

    md = render_markdown(report)
    if args.out:
        Path(args.out).write_text(md, encoding="utf-8")
    if args.json_out:
        Path(args.json_out).write_text(report.to_json(), encoding="utf-8")
    if not args.out and not args.json_out:
        print(md)
    else:
        confirmed = sum(1 for v in verdicts if v.tier == Tier.CONFIRMED)
        likely = sum(1 for v in verdicts if v.tier == Tier.LIKELY)
        hypo = sum(1 for v in verdicts if v.tier == Tier.HYPOTHESIS)
        print(f"签名 {len(signatures)} 个 | CONFIRMED {confirmed} / LIKELY {likely} / "
              f"HYPOTHESIS {hypo} | 候选变更 {len(candidates)} 条")
        print(f"报告：{args.out or '-'}  JSON：{args.json_out or '-'}")
    return 0


def cmd_suggest_upstreams(args: argparse.Namespace) -> int:
    suggestions = upstream_discovery.suggest_upstreams(
        args.repo, args.workspace, args.group_prefix)
    if not suggestions:
        print("未发现可匹配的本地上游仓库（依赖不在 workspace 内，"
              "或需要调整 --group-prefix）", file=sys.stderr)
        return 0
    if args.json:
        print(json.dumps([s.__dict__ for s in suggestions],
                         ensure_ascii=False, indent=2))
    else:
        for s in suggestions:
            print(f"--upstream {s.name}={s.path}", end=" ")
            print(f"# 依赖 {s.group}:{s.artifact}")
    return 0


def cmd_kb_add(args: argparse.Namespace) -> int:
    kb = KnowledgeBase(args.kb)
    kb.add_entry(args.fingerprint, {
        "incident_id": args.incident_id, "date": args.date, "tier": args.tier,
        "root_cause": args.root_cause, "change_id": args.change_id,
        "notes": args.notes})
    print(f"已定案回写：{args.fingerprint} ← {args.incident_id}（{args.tier}）")
    return 0


def cmd_build_graph(args: argparse.Namespace) -> int:
    graph = code_graph.build_from_java_sources(args.java_src)
    Path(args.out).write_text(graph.to_json(), encoding="utf-8")
    print(f"节点 {len(graph.nodes)} 个，边 {sum(len(v) for v in graph.callees.values())} 条 → {args.out}")
    return 0


def cmd_audit_convert(args: argparse.Namespace) -> int:
    converter = audit_adapters.CONVERTERS[args.format]
    service_map = (json.loads(Path(args.service_map).read_text(encoding="utf-8"))
                   if args.service_map else None)
    entries = converter(
        json.loads(Path(args.input).read_text(encoding="utf-8")), service_map)
    existing = []
    if args.append and Path(args.out).exists():
        existing = json.loads(Path(args.out).read_text(encoding="utf-8"))
        known = {e.get("id") for e in existing}
        entries = [e for e in entries if e.get("id") not in known]
    merged = existing + entries
    Path(args.out).write_text(
        json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{args.format} → 新增 {len(entries)} 条（共 {len(merged)} 条）→ {args.out}")
    return 0


def cmd_graph_from_project_map(args: argparse.Namespace) -> int:
    graph = project_map_adapter.load_and_convert(args.symbols, args.calls or "")
    if args.java_src:
        # 内置方法级图补 project-map 缺失的模块内调用链
        graph = project_map_adapter.merge(
            code_graph.build_from_java_sources(args.java_src), graph)
    Path(args.out).write_text(graph.to_json(), encoding="utf-8")
    print(f"节点 {len(graph.nodes)} 个，边 {sum(len(v) for v in graph.callees.values())} 条 → {args.out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="rca", description="线上故障根因定位引擎")
    sub = p.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="执行完整 RCA 流程")
    run.add_argument("--alert", required=True, help="告警上下文 JSON")
    run.add_argument("--logs", nargs="+", help="日志文件（可多个）")
    run.add_argument("--log-cmd", help="日志导出命令（仅限操作者本人输入，勿拼接外部数据）")
    run.add_argument("--es-url", help="Elasticsearch 地址，如 http://es:9200")
    run.add_argument("--es-index", default="app-log-*")
    run.add_argument("--es-lookback-min", type=int, default=120,
                     help="从告警时间往前查多少分钟（默认 120）")
    run.add_argument("--es-service-field", default="",
                     help="服务名过滤字段（如 service.keyword），置空不过滤")
    run.add_argument("--es-query", default="", help="额外 query_string")
    run.add_argument("--es-timestamp-field", default="@timestamp")
    run.add_argument("--es-level-field", default="level")
    run.add_argument("--es-message-field", default="message")
    run.add_argument("--es-stack-field", default="stack_trace")
    run.add_argument("--es-size", type=int, default=2000)
    run.add_argument("--es-auth", default="", help="Basic 认证 user:pass")
    run.add_argument("--repo", help="服务代码仓库路径")
    run.add_argument("--upstream", action="append", default=[],
                     help="上游服务/共享库仓库 name=path[@commit]，可重复；"
                          "参与变更取证（relation=upstream）、图合并与版本锚定")
    run.add_argument("--stream-prefix-re",
                     help="多流交织日志的行前缀正则（第 1 捕获组为流 key），"
                          r"如 kubectl --prefix 输出用 '^\[pod/([^\]]+)\]\s?'")
    run.add_argument("--audit", help="配置/DB/基础设施变更审计 JSON")
    run.add_argument("--code-graph", help="预构建的 code graph JSON（如 project-map 产物）")
    run.add_argument("--kb", help="知识库 JSON 路径")
    run.add_argument("--write-back", action="store_true", help="裁决后回写知识库")
    run.add_argument("--window-hours", type=int, default=72)
    run.add_argument("--out", help="Markdown 报告输出路径")
    run.add_argument("--json-out", help="JSON 报告输出路径")
    run.set_defaults(fn=cmd_run)

    su = sub.add_parser("suggest-upstreams",
                        help="从 pom.xml 依赖推导 workspace 内的上游仓库清单")
    su.add_argument("--repo", required=True, help="服务代码仓库路径")
    su.add_argument("--workspace", required=True, help="存放各仓库的父目录")
    su.add_argument("--group-prefix", default="",
                    help="只看该 groupId 前缀的依赖（如 com.example）")
    su.add_argument("--json", action="store_true")
    su.set_defaults(fn=cmd_suggest_upstreams)

    ka = sub.add_parser("kb-add", help="定案回写：反驳验证通过后写入最终根因解释")
    ka.add_argument("--kb", required=True)
    ka.add_argument("--fingerprint", required=True)
    ka.add_argument("--incident-id", required=True)
    ka.add_argument("--date", required=True)
    ka.add_argument("--tier", required=True, choices=["CONFIRMED", "LIKELY"])
    ka.add_argument("--root-cause", required=True)
    ka.add_argument("--change-id", default="")
    ka.add_argument("--notes", default="")
    ka.set_defaults(fn=cmd_kb_add)

    bg = sub.add_parser("build-graph", help="从 Java 源码构建 code graph")
    bg.add_argument("--java-src", required=True)
    bg.add_argument("--out", required=True)
    bg.set_defaults(fn=cmd_build_graph)

    ac = sub.add_parser("audit-convert",
                        help="把 Apollo/Nacos/DDL 工单导出转成 audit.json")
    ac.add_argument("--format", required=True, choices=sorted(audit_adapters.CONVERTERS))
    ac.add_argument("--input", required=True, help="外部系统导出的 JSON")
    ac.add_argument("--out", required=True)
    ac.add_argument("--service-map", help="外部应用标识→服务名映射 JSON")
    ac.add_argument("--append", action="store_true",
                    help="按 id 去重后追加到已有 out 文件（多源合并）")
    ac.set_defaults(fn=cmd_audit_convert)

    gpm = sub.add_parser("graph-from-project-map",
                         help="project-map 索引（symbols/calls.json）转 code graph")
    gpm.add_argument("--symbols", required=True)
    gpm.add_argument("--calls", help="calls.json（可选，仅跨模块边）")
    gpm.add_argument("--java-src", help="同时用内置解析器补模块内调用链")
    gpm.add_argument("--out", required=True)
    gpm.set_defaults(fn=cmd_graph_from_project_map)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
