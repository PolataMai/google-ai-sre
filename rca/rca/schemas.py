"""数据契约：三条线之间传递的全部结构化对象。

约束（对应方案里的"不猜测"机制）：
- Verdict.tier == HYPOTHESIS 时禁止携带 change_id —— 由 validate_verdict 强制；
- CONFIRMED 只允许来自 DIRECT 机制（diff 与堆栈帧直接相交），
  且 explanation_required=True，行为差异解释必须由上层（agent）补齐后才算闭环。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class ChangeType(str, Enum):
    CODE = "code"
    CONFIG = "config"
    DB = "db"
    INFRA = "infra"


class Mechanism(str, Enum):
    DIRECT = "DIRECT"      # 变更 diff 与报错堆栈帧直接相交
    GRAPH = "GRAPH"        # 变更符号落在故障可达代码集内（code graph 交集）
    TEMPORAL = "TEMPORAL"  # 仅时间/服务维度相关（配置、DB、基础设施）
    NONE = "NONE"


class Tier(str, Enum):
    CONFIRMED = "CONFIRMED"
    LIKELY = "LIKELY"
    HYPOTHESIS = "HYPOTHESIS"


def parse_ts(ts: str) -> datetime:
    """解析 ISO 时间戳，统一归一为 UTC-naive 再比较。

    约定：全链路时间统一 UTC；无时区标注的时间（常见于日志）按 UTC 处理，
    生产接入时由日志 adapter 负责先归一时区。
    """
    dt = datetime.fromisoformat(ts.replace(" ", "T", 1).replace(",", "."))
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


@dataclass
class ErrorFrame:
    class_fqn: str
    method: str
    file: str          # e.g. PricingService.java
    line: int          # 0 表示 Unknown Source / Native Method
    is_business: bool = False

    @property
    def symbol(self) -> str:
        return f"{self.class_fqn}.{self.method}"


@dataclass
class ErrorSignature:
    fingerprint: str
    exception_type: str      # 最深层 Caused by 的异常类型（真正的根因异常）
    message_sample: str
    service: str
    top_business_frame: Optional[ErrorFrame]
    frames: list[ErrorFrame] = field(default_factory=list)
    first_seen: str = ""
    last_seen: str = ""
    count: int = 0

    @staticmethod
    def make_fingerprint(exception_type: str, top_frame: Optional[ErrorFrame]) -> str:
        # 指纹不含行号：行号跨版本漂移，指纹要求跨版本稳定
        frame_key = f"{top_frame.class_fqn}.{top_frame.method}@{top_frame.file}" if top_frame else "-"
        raw = f"{exception_type}|{frame_key}"
        return hashlib.sha1(raw.encode()).hexdigest()[:16]


@dataclass
class TouchedRegion:
    file: str            # 仓库相对路径
    line_start: int      # 变更后版本的行号区间
    line_end: int
    symbol_hint: str = ""  # git hunk header 里的函数上下文


@dataclass
class CandidateChange:
    change_id: str
    change_type: ChangeType
    service: str
    timestamp: str
    summary: str
    author: str = ""
    relation: str = "same-service"   # same-service | upstream | shared-lib | shared-db | other-service
    touched: list[TouchedRegion] = field(default_factory=list)
    keys: list[str] = field(default_factory=list)  # 配置键 / 表名等，用于与日志内容做词面匹配


@dataclass
class EvidenceLink:
    kind: str     # stack_frame | code_anchor | diff_hunk | graph_path | key_match | temporal
    detail: str
    source: str = ""


@dataclass
class ScoredCandidate:
    change_id: str
    mechanism: Mechanism
    evidence: list[EvidenceLink] = field(default_factory=list)
    hit_frame: Optional[ErrorFrame] = None  # DIRECT 命中的具体堆栈帧，供版本锚定


@dataclass
class Verdict:
    fingerprint: str
    tier: Tier
    mechanism: Mechanism
    change_id: Optional[str] = None
    evidence_chain: list[EvidenceLink] = field(default_factory=list)
    ranked_candidates: list[ScoredCandidate] = field(default_factory=list)
    explanation: str = ""            # diff 前后行为差异解释，由 agent 层补齐
    explanation_required: bool = False
    anchor_ok: bool = True           # 堆栈帧是否成功锚定到发布 commit 的源码行
    next_actions: list[str] = field(default_factory=list)  # HYPOTHESIS 的下一步排查动作


@dataclass
class KbEntry:
    fingerprint: str
    incident_id: str
    date: str
    tier: str
    root_cause: str
    change_id: str = ""
    notes: str = ""


@dataclass
class RcaReport:
    incident_id: str
    service: str
    alert_time: str
    deployed_commit: str
    signatures: list[ErrorSignature] = field(default_factory=list)
    candidates: list[CandidateChange] = field(default_factory=list)
    verdicts: list[Verdict] = field(default_factory=list)
    mitigation: list[str] = field(default_factory=list)     # 止血建议，与根因分析分离
    kb_matches: list[KbEntry] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2, default=str)


class GuardrailViolation(Exception):
    pass


def validate_verdict(v: Verdict) -> None:
    """"不猜测"守门：机制与结论分级、证据链的一致性硬校验。"""
    if v.tier == Tier.HYPOTHESIS:
        if v.change_id:
            raise GuardrailViolation(
                f"HYPOTHESIS 结论禁止归因到具体变更（change_id={v.change_id}）——这就是被禁止的'硬凑根因'")
        if not v.next_actions:
            raise GuardrailViolation("HYPOTHESIS 结论必须给出下一步排查动作")
    if v.tier == Tier.CONFIRMED:
        if v.mechanism != Mechanism.DIRECT:
            raise GuardrailViolation(
                f"CONFIRMED 只能来自 DIRECT 机制，当前为 {v.mechanism}")
        kinds = {e.kind for e in v.evidence_chain}
        required = {"stack_frame", "diff_hunk"}
        if not required.issubset(kinds):
            raise GuardrailViolation(
                f"CONFIRMED 证据链不完整：缺少 {required - kinds}")
        if not v.change_id:
            raise GuardrailViolation("CONFIRMED 必须归因到具体变更")
    if v.tier == Tier.LIKELY and not v.change_id:
        raise GuardrailViolation("LIKELY 必须归因到具体变更")
