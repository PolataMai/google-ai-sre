"""RCA 报告渲染：止血建议与根因分析显式分离，证据链逐条列出。"""
from __future__ import annotations

from .schemas import RcaReport, Tier, Verdict

_TIER_BADGE = {
    Tier.CONFIRMED: "🔴 CONFIRMED",
    Tier.LIKELY: "🟡 LIKELY",
    Tier.HYPOTHESIS: "⚪ HYPOTHESIS",
}


def render_markdown(report: RcaReport) -> str:
    lines: list[str] = []
    add = lines.append
    add(f"# RCA 报告 — {report.incident_id}")
    add("")
    add(f"- 服务：`{report.service}`　告警时间：{report.alert_time}")
    add(f"- 发布版本：`{report.deployed_commit[:12] or '未提供'}`")
    add("")

    add("## 一、止血建议（优先执行）")
    add("")
    for m in report.mitigation:
        add(f"- {m}")
    add("")

    add("## 二、根因结论")
    add("")
    sig_by_fp = {s.fingerprint: s for s in report.signatures}
    for v in report.verdicts:
        sig = sig_by_fp.get(v.fingerprint)
        title = f"{sig.exception_type}" if sig else v.fingerprint
        add(f"### {_TIER_BADGE[v.tier]} — {title}")
        add("")
        if sig:
            tbf = sig.top_business_frame
            loc = f"{tbf.symbol}({tbf.file}:{tbf.line})" if tbf else "（无业务帧）"
            add(f"- 错误位置：`{loc}`")
            add(f"- 首次出现：{sig.first_seen}　次数：{sig.count}")
            add(f"- 样本信息：{sig.message_sample or '-'}")
        if v.change_id:
            add(f"- 归因变更：`{v.change_id[:12]}`（机制：{v.mechanism.value}）")
        if not v.anchor_ok:
            add("- ⚠️ 版本锚定失败：堆栈行号与发布 commit 源码不一致，结论已降级")
        if v.evidence_chain:
            add("- 证据链：")
            for e in v.evidence_chain:
                add(f"  1. [{e.kind}] {e.detail}")
        if v.explanation:
            add(f"- 行为差异解释：{v.explanation}")
        elif v.explanation_required:
            add("- 行为差异解释：**待补齐**（须由分析者对照 diff 说明变更前后行为差异，"
                "并经反驳验证后方可闭环）")
        if v.next_actions:
            add("- 下一步排查动作：")
            for a in v.next_actions:
                add(f"  - {a}")
        add("")

    add("## 三、候选变更（窗口内全部）")
    add("")
    if report.candidates:
        add("| 变更 | 类型 | 服务 | 时间 | 关联路径 | 摘要 |")
        add("|---|---|---|---|---|---|")
        for c in report.candidates:
            add(f"| `{c.change_id[:12]}` | {c.change_type.value} | {c.service} "
                f"| {c.timestamp} | {c.relation} | {c.summary} |")
    else:
        add("窗口内未发现任何变更。")
    add("")

    if report.kb_matches:
        add("## 四、历史相似故障（知识库命中）")
        add("")
        for k in report.kb_matches:
            add(f"- {k.date} `{k.incident_id}`（{k.tier}）：{k.root_cause}"
                f"{'，当时归因变更 `' + k.change_id[:12] + '`' if k.change_id else ''}")
        add("")

    if report.warnings:
        add("## 告警与注意事项")
        add("")
        for w in report.warnings:
            add(f"- ⚠️ {w}")
        add("")

    return "\n".join(lines)
