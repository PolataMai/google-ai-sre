"""统一变更事件契约：五类变更源归一为 ChangeEvent，进同一条时间线。

设计要点：
- details 按 source 携带各自语义（commit/files、keys、tables、component），
  但风险画像与关联排序只依赖统一字段——新增变更源不改下游；
- 灰度/回滚是变更的"声明"，precheck 门负责核查声明与风险等级是否匹配；
- 时间统一 UTC（无时区标注按 UTC 处理，与 rca 引擎同约定）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class Source(str, Enum):
    CODE = "code"                # 代码发布（git commit / 发布单）
    CONFIG = "config"            # 配置中心变更
    DB = "db"                    # 数据库 DDL/DML 工单
    MIDDLEWARE = "middleware"    # MQ/缓存/ES 等中间件变更
    INFRA = "infra"              # 扩缩容/网络/证书/K8s 等基础设施变更


class Status(str, Enum):
    PLANNED = "planned"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    ROLLED_BACK = "rolled_back"


class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


def parse_ts(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts.replace(" ", "T", 1).replace(",", "."))
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


@dataclass
class ChangeEvent:
    change_id: str
    source: Source
    service: str
    timestamp: str               # 执行/计划执行时间，ISO UTC
    summary: str
    author: str = ""
    env: str = "prod"
    status: Status = Status.DONE
    gray: bool = False           # 声明：是否灰度/分批发布
    rollback_plan: str = ""      # 声明：回滚方式；空串 = 不具备回滚能力
    details: dict = field(default_factory=dict)
    links: dict = field(default_factory=dict)   # ticket / pipeline / mr 等外链

    def to_dict(self) -> dict:
        d = asdict(self)
        d["source"] = self.source.value
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ChangeEvent":
        d = dict(d)
        d["source"] = Source(d["source"])
        d["status"] = Status(d.get("status", "done"))
        return cls(**d)

    # ---- 供风险画像使用的统一"范围"视图 ----

    def scope_items(self) -> list[str]:
        det = self.details
        return list(det.get("files") or det.get("keys")
                    or det.get("tables") or det.get("components") or [])

    def is_ddl(self) -> bool:
        return self.source == Source.DB and bool(self.details.get("ddl", True))


@dataclass
class RiskFactor:
    name: str        # scope | blast_radius | core_link | history | coverage | timing | capability
    points: int
    evidence: str    # 人可读的证据，禁止空泛描述


@dataclass
class RiskProfile:
    change_id: str
    score: int
    level: RiskLevel
    factors: list[RiskFactor] = field(default_factory=list)
    blast_services: list[str] = field(default_factory=list)  # 影响到的下游服务清单

    def to_dict(self) -> dict:
        d = asdict(self)
        d["level"] = self.level.value
        return d


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)
