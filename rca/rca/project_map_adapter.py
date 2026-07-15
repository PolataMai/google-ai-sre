"""project-map 索引 → CodeGraph 转换器。

输入（/project-map skill 的 AST 级产物）：
- symbols.json：每个类型声明 {fqn, methods: [{name, line}], file, line, module}；
- calls.json  ：类级调用边 {from: 类fqn, to: 类fqn}（仅跨模块，best-effort）。

转换策略（与方案的"宁可偏大不能漏"一致）：
- 节点：每个方法一个节点，行区间 = [本方法行, 下一方法行-1]，
  最后一个方法用 max_method_span 截断（AST 未给出方法结束行）；
- 边：类级边展开为方法级全连接（过近似）；
- calls.json 只有跨模块边，模块内调用链缺失 → 提供 merge()，
  与内置 build_from_java_sources 的方法级图取并集，各补对方盲区。
"""
from __future__ import annotations

import json
from collections import defaultdict
from typing import Union

from .code_graph import CodeGraph, GraphNode


def convert(symbols: list[dict], calls: Union[list, dict],
            max_method_span: int = 400) -> CodeGraph:
    graph = CodeGraph()
    methods_by_class: dict[str, list[str]] = defaultdict(list)

    for decl in symbols:
        fqn = decl.get("fqn", "")
        file = decl.get("file", "")
        methods = sorted(
            (m for m in decl.get("methods", []) if m.get("name")),
            key=lambda m: int(m.get("line", 0)))
        for i, m in enumerate(methods):
            start = int(m.get("line", 0)) or 1
            if i + 1 < len(methods):
                end = max(int(methods[i + 1].get("line", 0)) - 1, start)
            else:
                end = start + max_method_span
            nid = f"{fqn}.{m['name']}"
            graph.add_node(GraphNode(id=nid, file=file,
                                     line_start=start, line_end=end))
            methods_by_class[fqn].append(nid)

    edges = calls.get("edges", []) if isinstance(calls, dict) else calls
    for e in edges:
        src_cls, dst_cls = e.get("from", ""), e.get("to", "")
        for src in methods_by_class.get(src_cls, ()):
            for dst in methods_by_class.get(dst_cls, ()):
                graph.add_edge(src, dst)
    return graph


def merge(base: CodeGraph, extra: CodeGraph) -> CodeGraph:
    """并集合并：节点以 base 优先（内置解析器行区间更准），边全并。"""
    for nid, node in extra.nodes.items():
        if nid not in base.nodes:
            base.add_node(node)
    for caller, callees in extra.callees.items():
        for callee in callees:
            base.add_edge(caller, callee)
    return base


def load_and_convert(symbols_path: str, calls_path: str = "",
                     max_method_span: int = 400) -> CodeGraph:
    from pathlib import Path
    symbols = json.loads(Path(symbols_path).read_text(encoding="utf-8"))
    calls: Union[list, dict] = []
    if calls_path:
        calls = json.loads(Path(calls_path).read_text(encoding="utf-8"))
    return convert(symbols, calls, max_method_span)
