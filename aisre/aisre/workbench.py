"""事故工作台(F05):按事故动态生成的单一视图。

验收标准是"首轮调查不需要跳转多个控制台":时间线(告警/发布/事实观测
对齐)、数据源状态(缺失明示)、事实(证据可回跳)、Top-3 假设、建议动作
(来自 Top-1 场景白名单;调查型场景明确提示无自动动作)集中在一个结构里,
可渲染成 Markdown 直接贴进事故平台或 IM。
"""
from __future__ import annotations

from aisre.enrichment import EnrichmentRun
from aisre.intake import Alert
from aisre.scenarios import get_scenario


def build_workbench(run: EnrichmentRun, alert: Alert) -> dict:
    enr = run.enrichment

    timeline = [{"at": enr.alert_received_at, "kind": "alert",
                 "text": f"告警接入:{alert.title}({alert.severity})"}]
    for ef in run.extracted:
        kind = "deploy" if ef.kind == "recent_deploy" else "fact"
        timeline.append({"at": ef.fact.observed_at, "kind": kind,
                         "text": ef.fact.text})
    timeline.sort(key=lambda e: e["at"])

    facts = []
    for f in enr.facts:
        urls = [enr.evidences[eid].url for eid in f.evidence_ids
                if eid in enr.evidences]
        facts.append({"fact_id": f.fact_id, "text": f.text,
                      "observed_at": f.observed_at, "evidence_urls": urls})

    hypotheses = [{"rank": h.rank, "cause_code": h.cause_code,
                   "confidence": h.confidence,
                   "evidence_for": h.evidence_for,
                   "evidence_against": h.evidence_against,
                   "verification_steps": h.verification_steps}
                  for h in enr.hypotheses]

    top_scenario = get_scenario(enr.hypotheses[0].cause_code)
    suggested_actions = [{"action_type": action, "service": run.service}
                         for action in top_scenario.allowed_actions]
    action_note = ("按场景白名单生成,执行前需通过动作契约校验与审批"
                   if suggested_actions
                   else "该场景无自动动作,需人工调查(参照验证步骤)")

    return {
        "incident": {"incident_id": enr.incident_id,
                     "service": run.service,
                     "severity": alert.severity,
                     "title": alert.title,
                     "alert_received_at": enr.alert_received_at,
                     "enrichment_published_at": enr.enrichment_published_at,
                     "partial": run.partial},
        "timeline": timeline,
        "data_sources": [{"source": r.source, "status": r.status,
                          "error": r.error} for r in run.results],
        "facts": facts,
        "hypotheses": hypotheses,
        "suggested_actions": suggested_actions,
        "action_note": action_note,
    }


def render_markdown(wb: dict) -> str:
    inc = wb["incident"]
    lines = [
        f"# 事故 {inc['incident_id']} — {inc['service']}",
        f"告警:{inc['title']}(severity={inc['severity']}),"
        f"接入 {inc['alert_received_at']},发布丰富 {inc['enrichment_published_at']}"
        + ("(部分结果,存在缺失源)" if inc["partial"] else ""),
        "",
        "## 时间线",
    ]
    for e in wb["timeline"]:
        lines.append(f"- `{e['at']}` [{e['kind']}] {e['text']}")

    lines += ["", "## 数据源"]
    for s in wb["data_sources"]:
        note = f":{s['error']}" if s["error"] else ""
        lines.append(f"- {s['source']}: {s['status']}{note}")

    lines += ["", "## 事实(全部带证据)"]
    for f in wb["facts"]:
        urls = " ".join(f"[证据]({u})" for u in f["evidence_urls"])
        lines.append(f"- **{f['fact_id']}** {f['text']} {urls}")

    lines += ["", "## Top-3 假设"]
    for h in wb["hypotheses"]:
        lines.append(
            f"{h['rank']}. **{h['cause_code']}**(置信 {h['confidence']:.2f})"
            f" 支持: {h['evidence_for'] or '无'}"
            f" 反对: {h['evidence_against'] or '无'}")
        lines.append(f"   验证步骤: {', '.join(h['verification_steps'])}")

    lines += ["", "## 建议动作"]
    if wb["suggested_actions"]:
        for a in wb["suggested_actions"]:
            lines.append(f"- `{a['action_type']}` → {a['service']}")
    lines.append(f"> {wb['action_note']}")

    return "\n".join(lines)
