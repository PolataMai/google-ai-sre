"""Code graph：方法级调用图，用于把"泛泛而谈"变成集合运算。

两种来源：
- build_from_java_sources：内置的轻量 Java 解析器（正则 + 行区间），
  调用边按方法简单名解析，是有意的过近似（over-approximation）——
  可达集宁可偏大（多出 LIKELY 候选），不能漏（漏掉真根因）；
- from_json / to_json：可插拔契约，生产上可直接挂 project-map 或
  字节码级调用图（如 jdeps/soot 产物）替换内置解析器。
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

from .schemas import ErrorFrame, TouchedRegion

PACKAGE_RE = re.compile(r"^\s*package\s+([\w.]+)\s*;")
CLASS_RE = re.compile(r"^\s*(?:public\s+|final\s+|abstract\s+)*(?:class|interface|enum|record)\s+(\w+)")
METHOD_RE = re.compile(
    r"^\s*(?:(?:public|private|protected|static|final|synchronized|abstract|native|default)\s+)+"
    r"(?:<[^>]+>\s+)?[\w.<>\[\],\s?]+?\s+(\w+)\s*\(([^)]*)\)[\w\s.,]*\{")
# 捕获紧邻 '(' 的最后一个标识符段：`svc.applyCoupon(` → applyCoupon
CALL_RE = re.compile(r"(\w+)\s*\(")
NON_CALL_TOKENS = frozenset({
    "if", "for", "while", "switch", "catch", "return", "new", "super",
    "this", "synchronized", "throw", "assert", "do", "else", "try"})


@dataclass
class GraphNode:
    id: str            # com.example.order.service.PricingService.applyCoupon
    file: str          # 仓库相对路径
    line_start: int
    line_end: int


@dataclass
class CodeGraph:
    nodes: dict[str, GraphNode] = field(default_factory=dict)
    callees: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    callers: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))

    def add_node(self, node: GraphNode) -> None:
        self.nodes[node.id] = node

    def add_edge(self, caller: str, callee: str) -> None:
        if caller == callee:
            return
        self.callees[caller].add(callee)
        self.callers[callee].add(caller)

    # ---- 查询 ----

    def resolve_frame(self, frame: ErrorFrame) -> Optional[str]:
        nid = f"{frame.class_fqn}.{frame.method}"
        return nid if nid in self.nodes else None

    def nodes_touching(self, region: TouchedRegion) -> list[GraphNode]:
        """变更区域 → 落在其行区间内的图节点（文件按后缀匹配）。"""
        hits = []
        for node in self.nodes.values():
            if not (node.file == region.file or node.file.endswith("/" + region.file)
                    or region.file.endswith("/" + node.file)):
                continue
            if region.line_start <= node.line_end and node.line_start <= region.line_end:
                hits.append(node)
        return hits

    def reachable(self, seeds: list[str], up_depth: int = 5, down_depth: int = 2) -> set[str]:
        """故障可达代码集：先沿调用方上溯 up_depth 层得到调用路径，
        再从整条路径（种子 ∪ 祖先）下钻 down_depth 层。

        下钻覆盖祖先的旁支被调方：祖先调用的其他方法可能制造了
        导致报错点故障的状态（如提前污染入参），漏掉即漏根因。
        """
        result = set(s for s in seeds if s in self.nodes)
        frontier = set(result)
        for _ in range(up_depth):
            frontier = {c for n in frontier for c in self.callers.get(n, ())} - result
            result |= frontier
            if not frontier:
                break
        frontier = set(result)  # 种子 + 全部祖先
        for _ in range(down_depth):
            frontier = {c for n in frontier for c in self.callees.get(n, ())} - result
            result |= frontier
            if not frontier:
                break
        return result

    # ---- 序列化（可插拔契约）----

    def to_json(self) -> str:
        return json.dumps({
            "nodes": [asdict(n) for n in self.nodes.values()],
            "edges": [{"caller": a, "callee": b}
                      for a, bs in self.callees.items() for b in sorted(bs)],
        }, ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "CodeGraph":
        data = json.loads(text)
        g = cls()
        for n in data.get("nodes", []):
            g.add_node(GraphNode(**n))
        for e in data.get("edges", []):
            g.add_edge(e["caller"], e["callee"])
        return g


def _parse_java_file(path: Path, rel: str, graph: CodeGraph) -> list[tuple[str, int, int, list[str]]]:
    """解析单个 .java：注册节点，返回 (node_id, start, end, body_lines) 供第二遍连边。"""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    package, cls = "", ""
    methods: list[tuple[str, int]] = []  # (node_id, decl_line_no 1-based)
    for i, line in enumerate(lines, 1):
        if not package:
            m = PACKAGE_RE.match(line)
            if m:
                package = m.group(1)
                continue
        if not cls:
            m = CLASS_RE.match(line)
            if m:
                cls = m.group(1)
                continue
        m = METHOD_RE.match(line)
        if m and cls and m.group(1) not in NON_CALL_TOKENS:
            fqn = f"{package}.{cls}" if package else cls
            methods.append((f"{fqn}.{m.group(1)}", i))

    out = []
    for idx, (nid, start) in enumerate(methods):
        end = methods[idx + 1][1] - 1 if idx + 1 < len(methods) else len(lines)
        graph.add_node(GraphNode(id=nid, file=rel, line_start=start, line_end=end))
        out.append((nid, start, end, lines[start:end]))  # 体：声明行之后
    return out


def build_from_java_sources(root: str) -> CodeGraph:
    graph = CodeGraph()
    root_path = Path(root)
    parsed: list[tuple[str, int, int, list[str]]] = []
    for p in sorted(root_path.rglob("*.java")):
        rel = p.relative_to(root_path).as_posix()
        parsed.extend(_parse_java_file(p, rel, graph))

    # 简单名 → 节点集合（过近似解析）
    by_simple: dict[str, list[str]] = defaultdict(list)
    for nid in graph.nodes:
        by_simple[nid.rsplit(".", 1)[1]].append(nid)

    for nid, _start, _end, body in parsed:
        for line in body:
            code = line.split("//", 1)[0]
            for m in CALL_RE.finditer(code):
                name = m.group(1)
                if name in NON_CALL_TOKENS:
                    continue
                for target in by_simple.get(name, ()):
                    graph.add_edge(nid, target)
    return graph
