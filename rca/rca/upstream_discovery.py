"""上游仓库自动推导：从服务仓的 Maven 依赖清单反查 workspace 里的共享库/上游仓。

原理：
1. 解析服务仓（含多模块）全部 pom.xml 的 <dependencies>，收集依赖坐标；
2. 扫描 workspace 目录下各仓库的 pom.xml，建立 artifactId → 仓库路径 索引；
3. 依赖 artifactId 命中某个本地仓库 → 该仓库是上游候选，
   输出可直接粘贴的 `--upstream name=path` 参数。

Gradle / 非本地依赖（仅存在于制品库的 jar）不在覆盖范围——那类上游的
发布版本应通过制品库元数据回溯到源仓库，属于发布系统集成的范畴。
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

SKIP_DIRS = {"target", "build", "out", ".git", ".idea", "node_modules", ".gradle"}


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find_child_text(elem: ET.Element, name: str) -> str:
    for child in elem:
        if _strip_ns(child.tag) == name and child.text:
            return child.text.strip()
    return ""


def _iter_poms(root: Path):
    for pom in sorted(root.rglob("pom.xml")):
        if any(part in SKIP_DIRS for part in pom.parts):
            continue
        yield pom


def _parse_pom(pom: Path) -> tuple[str, str, list[tuple[str, str]]]:
    """返回 (groupId, artifactId, dependencies[(groupId, artifactId)])。

    groupId 缺省时继承 <parent>；解析失败返回空值（坏 pom 不中断扫描）。
    安全：合法 pom.xml 从不含 DTD——解析前拒绝 <!DOCTYPE/<!ENTITY，
    以零依赖方式同时防 XXE 与实体膨胀（billion laughs）。
    """
    try:
        text = pom.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "", "", []
    upper = text[:4096].upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        return "", "", []
    try:
        proj = ET.fromstring(text)
    except ET.ParseError:
        return "", "", []
    group = _find_child_text(proj, "groupId")
    artifact = _find_child_text(proj, "artifactId")
    if not group:
        for child in proj:
            if _strip_ns(child.tag) == "parent":
                group = _find_child_text(child, "groupId")
                break
    deps: list[tuple[str, str]] = []
    for child in proj:
        if _strip_ns(child.tag) != "dependencies":
            continue
        for dep in child:
            if _strip_ns(dep.tag) != "dependency":
                continue
            deps.append((_find_child_text(dep, "groupId"),
                         _find_child_text(dep, "artifactId")))
    return group, artifact, deps


@dataclass
class UpstreamSuggestion:
    name: str        # 仓库目录名
    path: str        # 仓库绝对路径
    artifact: str    # 命中的依赖 artifactId
    group: str


def collect_dependencies(repo: str,
                         group_prefix: str = "") -> list[tuple[str, str]]:
    """服务仓（含多模块）的全部依赖坐标，排除仓内模块间的自依赖。"""
    root = Path(repo)
    own_artifacts: set[str] = set()
    all_deps: list[tuple[str, str]] = []
    for pom in _iter_poms(root):
        _g, artifact, deps = _parse_pom(pom)
        if artifact:
            own_artifacts.add(artifact)
        all_deps.extend(deps)
    seen: set[tuple[str, str]] = set()
    out = []
    for g, a in all_deps:
        if not a or a in own_artifacts or (g, a) in seen:
            continue
        if group_prefix and not g.startswith(group_prefix):
            continue
        seen.add((g, a))
        out.append((g, a))
    return out


def index_workspace(workspace: str, exclude: str = "") -> dict[str, tuple[str, str]]:
    """workspace 下各仓库的 artifactId → (仓库目录名, 仓库路径)。"""
    idx: dict[str, tuple[str, str]] = {}
    ws = Path(workspace)
    exclude_path = Path(exclude).resolve() if exclude else None
    for repo_dir in sorted(p for p in ws.iterdir() if p.is_dir()):
        if repo_dir.name in SKIP_DIRS:
            continue
        if exclude_path and repo_dir.resolve() == exclude_path:
            continue
        for pom in _iter_poms(repo_dir):
            _g, artifact, _deps = _parse_pom(pom)
            if artifact and artifact not in idx:
                idx[artifact] = (repo_dir.name, str(repo_dir.resolve()))
    return idx


def suggest_upstreams(repo: str, workspace: str,
                      group_prefix: str = "") -> list[UpstreamSuggestion]:
    deps = collect_dependencies(repo, group_prefix)
    idx = index_workspace(workspace, exclude=repo)
    out: list[UpstreamSuggestion] = []
    seen_repos: set[str] = set()
    for g, a in deps:
        hit = idx.get(a)
        if not hit or hit[1] in seen_repos:
            continue
        seen_repos.add(hit[1])
        out.append(UpstreamSuggestion(name=hit[0], path=hit[1], artifact=a, group=g))
    return out
