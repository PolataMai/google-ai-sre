"""服务依赖图：变更影响范围（爆炸半径）的计算基础。

两种来源：
- mall-dill knowledge/indexes/services.json（Feign 调用边 + MQ topic 产消边），
  这是 project knowledge base 的 derived 层产物，天然与代码零漂移；
- 通用 edges JSON：{"edges": [{"from": "a", "to": "b"}]}（from 依赖 to）。

方向约定：edge (a → b) 表示 a 依赖 b。b 变更时受影响的是 a——
爆炸半径 = 反向边的可达集。MQ 边：consumer 依赖 producer。
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path


def _norm(name: str) -> str:
    """mall-dill-order / mall-dill-product-service / mall-dill-order-api → order / product / order

    *-api 模块是服务的契约层：依赖它等于依赖该服务本身，归一到同一节点。
    """
    n = name.strip()
    n = re.sub(r"^mall-dill-", "", n)
    n = re.sub(r"-service$", "", n)
    n = re.sub(r"-api$", "", n)
    return n


class ServiceGraph:
    def __init__(self):
        self.deps: dict[str, set[str]] = defaultdict(set)        # a → 它依赖的服务
        self.dependents: dict[str, set[str]] = defaultdict(set)  # b → 依赖它的服务

    def add_edge(self, frm: str, to: str, kind: str = "") -> None:
        frm, to = _norm(frm), _norm(to)
        if frm == to or not frm or not to:
            return
        self.deps[frm].add(to)
        self.dependents[to].add(frm)

    def blast_radius(self, service: str, depth: int = 3) -> list[str]:
        """service 变更后可能受影响的下游（依赖它的服务），BFS 限深。"""
        service = _norm(service)
        seen: set[str] = set()
        frontier = {service}
        for _ in range(depth):
            frontier = {d for s in frontier for d in self.dependents.get(s, ())} - seen - {service}
            if not frontier:
                break
            seen |= frontier
        return sorted(seen)

    def dependencies_of(self, service: str, depth: int = 3) -> list[str]:
        """service 所依赖的上游可达集（异常关联时用：我异常，我的上游谁变了）。"""
        service = _norm(service)
        seen: set[str] = set()
        frontier = {service}
        for _ in range(depth):
            frontier = {d for s in frontier for d in self.deps.get(s, ())} - seen - {service}
            if not frontier:
                break
            seen |= frontier
        return sorted(seen)

    @classmethod
    def from_malldill_services(cls, path: str) -> "ServiceGraph":
        data = json.loads(Path(path).read_text(encoding="utf-8"))["services"]
        g = cls()
        # topic → producer 服务集合（先建生产索引再连消费边）
        producers: dict[str, set[str]] = defaultdict(set)
        for module, info in data.items():
            for t in info.get("topics_produced", []):
                producers[t].add(module)
        for module, info in data.items():
            # Feign："ProductApi -> mall-dill-product-service"，调用方依赖被调方
            for call in info.get("feign_calls", []):
                target = call.split("->")[-1].strip()
                g.add_edge(module, target, "feign")
            # MQ：消费方依赖生产方
            for t in info.get("topics_consumed", []):
                for p in producers.get(t, ()):
                    g.add_edge(module, p, f"mq:{t}")
        return g

    @classmethod
    def from_edges_json(cls, path: str) -> "ServiceGraph":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        g = cls()
        for e in data.get("edges", []):
            g.add_edge(e["from"], e["to"], e.get("kind", ""))
        return g

    @classmethod
    def from_project_map_calls(cls, path: str) -> "ServiceGraph":
        """project-map calls.json → 模块级依赖边（补进程内直调盲区）。

        单体/模块直调（如 mall-dill 的 order→product 进程内调用）不走 Feign/MQ，
        services.json 抓不到；project-map 的 AST 级 module_edges 正好补上。
        优先用 module_edges；老版本产物没有该字段时从类级 edges 聚合。
        边语义一致：from 依赖 to。confidence 为 best-effort，作为
        补充边并入（宁可多一条 LIKELY 级关联，不可漏真凶通路）。
        """
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        g = cls()
        module_edges = data.get("module_edges")
        if module_edges is None:
            seen = set()
            module_edges = []
            for e in data.get("edges", data if isinstance(data, list) else []):
                fm, tm = e.get("from_module"), e.get("to_module")
                if fm and tm and (fm, tm) not in seen:
                    seen.add((fm, tm))
                    module_edges.append({"from_module": fm, "to_module": tm})
        for e in module_edges:
            fm, tm = e.get("from_module"), e.get("to_module")
            if fm and tm:
                g.add_edge(fm, tm, "call")
        return g

    def merge(self, other: "ServiceGraph") -> "ServiceGraph":
        """并集合并（Feign/MQ 边 ∪ 进程内调用边），原地修改并返回 self。"""
        for frm, tos in other.deps.items():
            for to in tos:
                self.add_edge(frm, to)
        return self
