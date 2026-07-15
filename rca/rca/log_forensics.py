"""线 1：日志取证。

原始日志 → 结构化 ErrorSignature 列表：
- 解析 Java 堆栈（含 Caused by 链，取最深层作为根因异常）；
- 过滤框架帧，定位顶层业务帧（文件:行号）；
- 按指纹（异常类型 + 顶层业务帧符号，不含行号）聚类，
  产出 first_seen（与线 2 对齐的 join key）、last_seen、count。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .schemas import ErrorFrame, ErrorSignature, parse_ts

# 常见框架/运行时包前缀：这些帧不是业务根因位置
FRAMEWORK_PREFIXES = (
    "java.", "javax.", "jakarta.", "jdk.", "sun.", "com.sun.",
    "org.springframework.", "org.apache.", "org.eclipse.", "org.junit.",
    "com.alibaba.dubbo.", "org.apache.dubbo.", "io.netty.", "io.grpc.",
    "com.mysql.", "org.mybatis.", "com.baomidou.", "org.hibernate.",
    "feign.", "okhttp3.", "retrofit2.", "ch.qos.logback.", "org.slf4j.",
    "com.fasterxml.", "com.google.", "reactor.", "kotlin.", "scala.",
)

TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)")
LEVEL_RE = re.compile(r"\b(ERROR|WARN|INFO|DEBUG|FATAL)\b")
# 异常首行：com.foo.BarException: message  （行首或 "Caused by: " 之后）
EXC_RE = re.compile(r"^(?:Caused by:\s+)?([\w$.]+(?:Exception|Error|Throwable))(?::\s?(.*))?$")
# 堆栈帧：at [module/]com.foo.Bar.method(Bar.java:42) | (Native Method) | (Unknown Source)
FRAME_RE = re.compile(
    r"^\s*at\s+(?:[\w.]+/)?([\w$.]+)\.([\w$<>]+)\(([^)]*)\)")


def _is_framework(class_fqn: str) -> bool:
    return class_fqn.startswith(FRAMEWORK_PREFIXES)


def _classify_frame(class_fqn: str, business_packages: list[str]) -> bool:
    if business_packages:
        return any(class_fqn.startswith(p) for p in business_packages)
    return not _is_framework(class_fqn)


def _parse_location(loc: str) -> tuple[str, int]:
    if ":" in loc:
        f, _, ln = loc.rpartition(":")
        if ln.isdigit():
            return f, int(ln)
    return loc or "Unknown", 0


@dataclass
class ErrorEvent:
    timestamp: str
    service: str
    exception_type: str
    message: str
    frames: list[ErrorFrame] = field(default_factory=list)


class _StackAccumulator:
    """把一段连续的异常文本（含 Caused by 链）折叠成'最深层原因'。"""

    def __init__(self, business_packages: list[str]):
        self.business_packages = business_packages
        self.exception_type = ""
        self.message = ""
        self.frames: list[ErrorFrame] = []
        self._active = False

    def feed_exception(self, exc_type: str, message: str) -> None:
        # 每遇到一层新的 Caused by 就整体替换：最终保留最深层
        self.exception_type = exc_type
        self.message = message or ""
        self.frames = []
        self._active = True

    def feed_frame(self, class_fqn: str, method: str, loc: str) -> None:
        if not self._active:
            return
        file, line = _parse_location(loc)
        self.frames.append(ErrorFrame(
            class_fqn=class_fqn, method=method, file=file, line=line,
            is_business=_classify_frame(class_fqn, self.business_packages)))

    @property
    def active(self) -> bool:
        return self._active

    def flush(self) -> Optional[tuple[str, str, list[ErrorFrame]]]:
        if not self._active or not self.exception_type:
            self._active = False
            return None
        result = (self.exception_type, self.message, list(self.frames))
        self.exception_type, self.message, self.frames = "", "", []
        self._active = False
        return result


def parse_error_events(text: str, service: str,
                       business_packages: Optional[list[str]] = None) -> list[ErrorEvent]:
    """从日志文本中抽取所有带堆栈的 ERROR 事件。"""
    business_packages = business_packages or []
    events: list[ErrorEvent] = []
    current_ts = ""
    pending_ts = ""       # 触发本次堆栈的 ERROR 日志行时间
    acc = _StackAccumulator(business_packages)

    def close_stack():
        nonlocal events
        result = acc.flush()
        if result:
            exc_type, message, frames = result
            events.append(ErrorEvent(
                timestamp=pending_ts or current_ts, service=service,
                exception_type=exc_type, message=message, frames=frames))

    for line in text.splitlines():
        ts_m = TS_RE.match(line)
        if ts_m:
            # 新的日志行：若上一个堆栈未闭合，先闭合
            if acc.active:
                close_stack()
            current_ts = ts_m.group(1)
            lvl = LEVEL_RE.search(line)
            if lvl and lvl.group(1) in ("ERROR", "FATAL"):
                pending_ts = current_ts
            else:
                pending_ts = ""
            continue

        frame_m = FRAME_RE.match(line)
        if frame_m:
            acc.feed_frame(frame_m.group(1), frame_m.group(2), frame_m.group(3))
            continue

        stripped = line.strip()
        if stripped.startswith("..."):
            continue  # "... 23 common frames omitted"
        exc_m = EXC_RE.match(stripped)
        if exc_m:
            acc.feed_exception(exc_m.group(1), exc_m.group(2) or "")
            continue

    if acc.active:
        close_stack()
    return events


def top_business_frame(frames: list[ErrorFrame]) -> Optional[ErrorFrame]:
    for f in frames:
        if f.is_business:
            return f
    return None


def cluster_events(events: list[ErrorEvent]) -> list[ErrorSignature]:
    """按指纹聚类，first_seen 升序输出。"""
    clusters: dict[str, ErrorSignature] = {}
    for ev in events:
        tbf = top_business_frame(ev.frames)
        fp = ErrorSignature.make_fingerprint(ev.exception_type, tbf)
        sig = clusters.get(fp)
        if sig is None:
            clusters[fp] = ErrorSignature(
                fingerprint=fp, exception_type=ev.exception_type,
                message_sample=ev.message, service=ev.service,
                top_business_frame=tbf, frames=ev.frames,
                first_seen=ev.timestamp, last_seen=ev.timestamp, count=1)
        else:
            sig.count += 1
            if parse_ts(ev.timestamp) < parse_ts(sig.first_seen):
                sig.first_seen = ev.timestamp
            if parse_ts(ev.timestamp) > parse_ts(sig.last_seen):
                sig.last_seen = ev.timestamp
    return sorted(clusters.values(), key=lambda s: (parse_ts(s.first_seen), -s.count))


def demultiplex(text: str, stream_re: str) -> list[str]:
    """把多流交织的日志按行前缀拆成独立流文本，并剥掉前缀。

    适用于 `kubectl logs --prefix`、docker compose 等每行带来源标记的收集器输出
    （如 `[pod/order-7d9f] 2026-07-11 ...`）——不同 pod 的多行堆栈互相穿插时，
    直接解析会把 A 流的帧误挂到 B 流的异常上。
    正则的第 1 个捕获组作为流 key；不带前缀的行归入上一条匹配行所在的流（尽力而为）。
    注意：前缀只打在事件首行、堆栈行无任何标记的交织（单文件多进程混写）在文本层
    不可恢复——该场景请走 ES 源（每文档自含完整堆栈）。
    """
    pattern = re.compile(stream_re)
    streams: dict[str, list[str]] = {}
    current_key = ""
    for line in text.splitlines():
        m = pattern.match(line)
        if m:
            current_key = m.group(1) if m.groups() else m.group(0)
            line = line[m.end():]
        streams.setdefault(current_key, []).append(line)
    return ["\n".join(lines) for lines in streams.values()]


def analyze_log(text: str, service: str,
                business_packages: Optional[list[str]] = None,
                stream_re: Optional[str] = None) -> list[ErrorSignature]:
    if stream_re:
        events: list[ErrorEvent] = []
        for chunk in demultiplex(text, stream_re):
            events += parse_error_events(chunk, service, business_packages)
        return cluster_events(events)
    return cluster_events(parse_error_events(text, service, business_packages))
