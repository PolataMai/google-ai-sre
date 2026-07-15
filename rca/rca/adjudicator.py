"""线 3：根因裁决器。

"不猜测"的机制化实现：
1. 集合运算代替自由发挥——
   DIRECT：变更 diff 行区间与报错堆栈帧(文件:行号)直接相交；
   GRAPH：变更符号 ∈ 故障可达代码集（从报错帧沿 code graph 上溯/下钻）；
   TEMPORAL：仅时间/服务/关键词维度相关（配置、DB、基础设施变更）。
2. 证据链强制 + 结论分级——
   DIRECT→CONFIRMED（还需 agent 层补齐行为差异解释 + 反驳验证）；
   GRAPH/TEMPORAL→LIKELY；无交集→HYPOTHESIS，禁止归因任何变更，
   必须给出下一步排查动作（无变更故障的 fallback）。
3. 版本锚定——报错帧回查发布 commit 的真实源码行，锚定失败（版本漂移）
   则 CONFIRMED 降级为 LIKELY 并告警。
"""
from __future__ import annotations

from typing import Optional

from . import change_sources
from .code_graph import CodeGraph
from .schemas import (CandidateChange, ChangeType, ErrorFrame, ErrorSignature,
                      EvidenceLink, Mechanism, ScoredCandidate, Tier, Verdict,
                      validate_verdict)

# 无变更交集时的兜底排查清单（无变更故障：流量/数据/第三方/周期任务/资源）
DEFAULT_NEXT_ACTIONS = [
    "核对故障时段流量与 QPS 是否突增（入口限流指标、网关日志）",
    "检查上游依赖与第三方服务健康度（超时率、熔断状态）",
    "检查数据侧：表数据量增长、慢查询、锁等待",
    "排查周期性任务：故障起点是否与 cron / 批处理窗口重合",
    "检查证书、配额、连接池等会随时间耗尽的资源",
    "扩大变更窗口重查，并确认是否存在未纳管的变更源（手工改配置、直连改库）",
]

_RELATED_RELATIONS = {"same-service", "upstream", "shared-lib", "shared-db"}


def _file_match(region_file: str, frame_file: str) -> bool:
    return (region_file == frame_file
            or region_file.endswith("/" + frame_file)
            or frame_file.endswith("/" + region_file))


def _tokens(text: str) -> set[str]:
    out, cur = set(), []
    for ch in text:
        if ch.isalnum() or ch in "._-":
            cur.append(ch)
        elif cur:
            out.add("".join(cur).lower())
            cur = []
    if cur:
        out.add("".join(cur).lower())
    return out


def anchor_frame(repos: list[tuple[str, str]],
                 frame: ErrorFrame) -> tuple[Optional[EvidenceLink], bool]:
    """把堆栈帧锚定到发布 commit 的真实源码行（依次尝试各登记仓库）。

    repos: [(仓库路径, 发布 commit)]，主仓在前、上游仓在后；
    返回 (证据, 是否锚定成功)。无任何仓库信息时不做锚定，不算失败。
    """
    usable = [(r, c) for r, c in repos if r]
    if not usable or frame.line <= 0:
        return None, True
    for repo, commit in usable:
        ref = commit or "HEAD"
        path = change_sources.resolve_repo_path(repo, ref, frame.file)
        if not path:
            continue
        content = change_sources.read_file_at(repo, ref, path)
        if content is None:
            continue
        lines = content.splitlines()
        if frame.line > len(lines):
            continue
        src = lines[frame.line - 1].strip()
        return EvidenceLink(
            kind="code_anchor",
            detail=f"{path}:{frame.line} @ {ref[:10]} → `{src}`",
            source="git"), True
    return None, False


def score_candidate(sig: ErrorSignature, cand: CandidateChange,
                    graph: CodeGraph, reach: set[str]) -> ScoredCandidate:
    """对单个候选变更做机制判定，附带证据。"""
    business_frames = [f for f in sig.frames if f.is_business and f.line > 0]

    if cand.change_type == ChangeType.CODE:
        # DIRECT：diff 行区间 × 堆栈帧 直接相交
        for region in cand.touched:
            for frame in business_frames:
                if not _file_match(region.file, frame.file):
                    continue
                if region.line_start <= frame.line <= region.line_end:
                    return ScoredCandidate(cand.change_id, Mechanism.DIRECT, [
                        EvidenceLink("stack_frame",
                                     f"{frame.symbol}({frame.file}:{frame.line})", "log"),
                        EvidenceLink("diff_hunk",
                                     f"{cand.change_id[:10]} 改动 {region.file}:"
                                     f"{region.line_start}-{region.line_end}"
                                     f"{'（' + region.symbol_hint + '）' if region.symbol_hint else ''}",
                                     "git"),
                    ], hit_frame=frame)
                if region.symbol_hint and frame.method in region.symbol_hint:
                    return ScoredCandidate(cand.change_id, Mechanism.DIRECT, [
                        EvidenceLink("stack_frame",
                                     f"{frame.symbol}({frame.file}:{frame.line})", "log"),
                        EvidenceLink("diff_hunk",
                                     f"{cand.change_id[:10]} 改动函数 {region.symbol_hint}"
                                     f"（{region.file}）", "git"),
                    ], hit_frame=frame)
        # GRAPH：变更符号落在故障可达代码集
        for region in cand.touched:
            for node in graph.nodes_touching(region):
                if node.id in reach:
                    return ScoredCandidate(cand.change_id, Mechanism.GRAPH, [
                        EvidenceLink("graph_path",
                                     f"变更符号 {node.id} ∈ 故障可达代码集"
                                     f"（自报错帧沿调用图可达）", "code-graph"),
                        EvidenceLink("diff_hunk",
                                     f"{cand.change_id[:10]} 改动 {region.file}:"
                                     f"{region.line_start}-{region.line_end}", "git"),
                    ])
        return ScoredCandidate(cand.change_id, Mechanism.NONE, [])

    # 配置 / DB / 基础设施变更：时间 + 关联路径 + 关键词
    if cand.relation not in _RELATED_RELATIONS:
        return ScoredCandidate(cand.change_id, Mechanism.NONE, [])
    evidence = [EvidenceLink(
        "temporal",
        f"{cand.change_type.value} 变更 {cand.change_id}（{cand.timestamp}）"
        f"先于错误首现（{sig.first_seen}），关联路径={cand.relation}", "audit")]
    log_tokens = _tokens(sig.message_sample) | _tokens(
        sig.top_business_frame.symbol if sig.top_business_frame else "")
    hit_keys = [k for k in cand.keys if k.lower() in log_tokens]
    if hit_keys:
        evidence.append(EvidenceLink(
            "key_match", f"变更键 {hit_keys} 出现在错误信息/报错符号中", "audit"))
    return ScoredCandidate(cand.change_id, Mechanism.TEMPORAL, evidence)


_RANK = {Mechanism.DIRECT: 0, Mechanism.GRAPH: 1, Mechanism.TEMPORAL: 2, Mechanism.NONE: 9}


def adjudicate(sig: ErrorSignature, candidates: list[CandidateChange],
               graph: CodeGraph, repo: Optional[str] = None,
               deployed_commit: str = "",
               warnings: Optional[list] = None,
               extra_repos: Optional[list[tuple[str, str]]] = None) -> Verdict:
    """对单个错误签名给出裁决。

    extra_repos：上游服务/共享库仓库 [(路径, 发布commit)]，参与版本锚定——
    堆栈帧指向上游代码时在上游仓里锚定（跨仓 fan-out）。
    """
    warnings = warnings if warnings is not None else []
    anchor_repos = ([(repo, deployed_commit)] if repo else []) + (extra_repos or [])
    seeds = [nid for f in sig.frames if f.is_business
             for nid in [graph.resolve_frame(f)] if nid]
    reach = graph.reachable(seeds)

    scored = [score_candidate(sig, c, graph, reach) for c in candidates]
    # TEMPORAL 中带 key_match 的优先于纯时间相关
    def sort_key(s: ScoredCandidate):
        boost = 0 if any(e.kind == "key_match" for e in s.evidence) else 1
        return (_RANK[s.mechanism], boost)
    hits = sorted([s for s in scored if s.mechanism != Mechanism.NONE], key=sort_key)

    if not hits:
        v = Verdict(fingerprint=sig.fingerprint, tier=Tier.HYPOTHESIS,
                    mechanism=Mechanism.NONE, change_id=None,
                    next_actions=list(DEFAULT_NEXT_ACTIONS))
        validate_verdict(v)
        return v

    best = hits[0]
    evidence = list(best.evidence)
    anchor_ok = True

    if best.mechanism == Mechanism.DIRECT:
        # 版本锚定：DIRECT 命中的那一帧必须能对上发布 commit 的源码行
        hit_frame = best.hit_frame or next(
            (f for f in sig.frames if f.is_business and f.line > 0), None)
        if hit_frame is not None:
            link, anchor_ok = anchor_frame(anchor_repos, hit_frame)
            if link:
                evidence.insert(1, link)
        if anchor_ok:
            tier = Tier.CONFIRMED
        else:
            tier = Tier.LIKELY
            warnings.append(
                f"[{sig.fingerprint}] 堆栈帧无法锚定到任何已登记仓库的发布版本源码行"
                f"（版本漂移？仓库未登记？），DIRECT 命中降级为 LIKELY——请核实线上实际运行版本")
    else:
        tier = Tier.LIKELY

    v = Verdict(fingerprint=sig.fingerprint, tier=tier, mechanism=best.mechanism,
                change_id=best.change_id, evidence_chain=evidence,
                ranked_candidates=hits, anchor_ok=anchor_ok,
                explanation_required=(tier == Tier.CONFIRMED))
    validate_verdict(v)
    return v


def build_mitigation(verdicts: list[Verdict],
                     candidates: list[CandidateChange]) -> list[str]:
    """止血建议——与根因分析分离，优先级高于修代码。"""
    by_id = {c.change_id: c for c in candidates}
    out: list[str] = []
    for v in verdicts:
        if not v.change_id:
            continue
        cand = by_id.get(v.change_id)
        desc = f"{v.change_id[:12]}（{cand.summary}）" if cand else v.change_id[:12]
        if v.tier == Tier.CONFIRMED:
            out.append(f"【立即】回滚变更 {desc} —— 已确认与报错堆栈直接相交，回滚优先于修代码")
        elif v.tier == Tier.LIKELY and cand and cand.change_type != ChangeType.CODE:
            out.append(f"【评估】回滚{cand.change_type.value}变更 {desc} —— 时间/关键词强相关")
        elif v.tier == Tier.LIKELY:
            out.append(f"【评估】灰度回滚变更 {desc} —— 位于故障可达代码集，需人工确认")
    if not out:
        out.append("【兜底】无可归因变更：按需启用限流/降级/扩容，并按下一步排查动作定位（见 HYPOTHESIS 结论）")
    return out
